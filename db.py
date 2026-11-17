"""SQLite per-root index store.

WAL + busy_timeout=5000ms + per-file single-writer lock (contract ⑦).
The build path batches commits every BUILD_COMMIT_BATCH files; the
hook/refresh path commits per file (gate ⑦ semantics preserved).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from .config import root_db_path, WAL_BUSY_TIMEOUT_MS, PARSER_GENERATION

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path          TEXT PRIMARY KEY,
    mtime         REAL NOT NULL,
    size          INTEGER NOT NULL,
    lang          TEXT NOT NULL,
    build_id      TEXT NOT NULL,
    fingerprint_epoch INTEGER NOT NULL DEFAULT 0,
    parser_gen    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS symbols (
    name  TEXT NOT NULL,
    kind  TEXT NOT NULL,
    line  INTEGER NOT NULL,
    sig   TEXT NOT NULL DEFAULT '',
    path  TEXT NOT NULL,
    root  TEXT NOT NULL,
    PRIMARY KEY (root, path, name, line)
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
-- L2.4 concept bridge (owner 2026-11-16): expression index for
-- prefix/exact candidate legs on large roots (SQLite can drive
-- lower(name) LIKE 'tok%' off it; leading-wildcard substring legs
-- remain scan-bound by design).
CREATE INDEX IF NOT EXISTS idx_symbols_lname ON symbols(lower(name));
-- L2.3b kind aggregation: drives WHERE kind=? ORDER BY path,line
-- (equality prefix + ordered tail, no sort at 35k-row scale) and the
-- subtree LIKE path-prefix leg (index range scan inside the kind).
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind, path, line);
-- L2.4d concept pre-filter (2026-11-18): the distinct lower-case name
-- vocabulary per root (~3k rows against a 62k-row symbols table). The
-- no-path/no-kind concept leg pre-filters the tier-3 substring net over
-- THIS table (O(distinct names)) instead of scanning every symbol row,
-- then JOINs back to symbols via idx_symbols_lname (the candidate set
-- is byte-identical: a name passes the net on the vocabulary iff some
-- row passes it). Maintained by insert_symbols (INSERT OR IGNORE) and
-- backfilled by init_db for legacy dbs; orphaned rows left by a full
-- rescan are JOIN-excluded (bounded, documented — the cost is one
-- stale row per ever-seen name, not a scan).
CREATE TABLE IF NOT EXISTS name_vocab (
    root  TEXT NOT NULL,
    lname TEXT NOT NULL,
    PRIMARY KEY (root, lname)
);
-- L2.4d composite index: per-name ranges WITHIN a root. Drives the
-- concept rank leg's vocab->symbols JOIN (the pre-L2.4d plan walked the
-- root's WHOLE symbols prefix via the PK + a per-row vocab probe —
-- 109ms at 62k rows) and the representative-row IN (62ms -> ~0ms:
-- each name is a (root, lower(name)) index range now).
CREATE INDEX IF NOT EXISTS idx_symbols_root_lname ON symbols(root, lower(name));
-- W1 (2026-11-17): equality probe for the EXACT branch (root=? AND name=?
-- AND kind=?). The case-SENSITIVE `name = ?` predicate cannot use the
-- lower(name) composite, so the planner fell back to the root-prefix of
-- idx_symbols_root_lname = a full-root row scan (32k rows ~ 180-230ms on
-- the live filemem db). This composite makes the equality probe O(log);
-- idempotent backfill on next init_db open (zero migration, F5 pattern).
CREATE INDEX IF NOT EXISTS idx_symbols_root_name_kind ON symbols(root, name, kind);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=WAL_BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=%d" % WAL_BUSY_TIMEOUT_MS)
    return conn


def init_db(root: str) -> sqlite3.Connection:
    db = root_db_path(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db)
    conn.executescript(_SCHEMA)
    # L2.3c in-place migration: pre-L2.3c dbs lack files.parser_gen
    # (CREATE TABLE IF NOT EXISTS cannot add a column to an existing
    # table); the legacy '' stamp = unknown generation, always re-parsed
    # on the next build (self-healing, no wipe ritual needed).
    cols = [r[1] for r in conn.execute("PRAGMA table_info(files)")]
    if "parser_gen" not in cols:
        conn.execute(
            "ALTER TABLE files ADD COLUMN parser_gen TEXT NOT NULL DEFAULT ''")
    # L2.4d one-shot backfill: legacy dbs carry symbols WITHOUT name_vocab
    # rows; populate them exactly once (gate = vocab still empty).
    # Fresh/maintained dbs skip (the gate is false), and orphaned vocab
    # rows from a full rescan stay harmless (JOIN-excluded, schema note).
    n_vocab = conn.execute("SELECT COUNT(*) FROM name_vocab").fetchone()[0]
    n_sym = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    if n_vocab == 0 and n_sym > 0:
        conn.execute(
            "INSERT OR IGNORE INTO name_vocab(root, lname)"
            " SELECT DISTINCT root, lower(name) FROM symbols")
    conn.commit()
    return conn


def upsert_file(conn: sqlite3.Connection, path: str, mtime: float,
                size: int, lang: str, build_id: str,
                commit: bool = True, parser_gen: str = "") -> None:
    """L2.3c: `parser_gen` stamps the PARSER GENERATION that parsed this
    file (config.PARSER_GENERATION) — a mismatch with the current
    generation forces a re-parse (scanner) and surfaces as
    staleness="parser_gen_stale" (queries). Default '' = legacy/unknown
    (pre-L2.3c rows): always treated as stale by the build."""
    conn.execute(
        "INSERT OR REPLACE INTO files(path, mtime, size, lang, build_id, "
        "fingerprint_epoch, parser_gen)"
        " VALUES(?, ?, ?, ?, ?, 0, ?)",
        (path, mtime, size, lang, build_id, parser_gen),
    )
    if commit:
        conn.commit()


def insert_symbols(conn: sqlite3.Connection, root: str, path: str,
                   rows: list, commit: bool = True) -> None:
    for name, kind, line, sig in rows:
        conn.execute(
            "INSERT OR REPLACE INTO symbols(name, kind, line, sig, path, root)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (name, kind, line, sig, path, root),
        )
    # L2.4d: keep the distinct-name vocabulary current (INSERT OR IGNORE —
    # idempotent across batches and rebuilds; deduped per batch).
    if rows:
        distinct = sorted({r[0] for r in rows})
        conn.executemany(
            "INSERT OR IGNORE INTO name_vocab(root, lname) VALUES (?, lower(?))",
            [(root, n) for n in distinct],
        )
    if commit:
        conn.commit()


def query_symbols(conn: sqlite3.Connection, root: str,
                  name: Optional[str] = None,
                  path: Optional[str] = None,
                  kind: Optional[str] = None,
                  limit: int = 200,
                  offset: int = 0,
                  path_prefix: Optional[str] = None,
                  order: str = "line") -> list:
    """Query symbol rows for `root`. `path` must be an ABSOLUTE file path —
    it matches EXACTLY one indexed file (the symbols table stores absolute
    paths, see insert_symbols / _reindex_single_file_sync). A relative or
    suffix-style path used to under-match or over-match sibling files with
    the same basename (the b-1 attach contamination: `__init__.py` pulled
    in every subpackage's __init__ symbols); exact match removes both.

    L2.3b: `path_prefix` (an absolute directory + os.sep, built by the
    caller) selects the SUBTREE via LIKE — the kind-aggregation branch's
    directory narrowing. Never set together with `path` (exact + subtree
    are mutually exclusive). `order` is an INTERNAL ordering hint
    (never user input): cross-file kind aggregation passes "path, line"
    (line-only ordering ties across files make OFFSET paging unstable);
    the legacy paths keep "line" byte-identical."""
    query = "SELECT root, path, name, kind, line, sig FROM symbols WHERE root = ?"
    params: list = [root]
    if name:
        query += " AND name = ?"
        params.append(name)
    if path:
        query += " AND path = ?"
        params.append(os.path.abspath(path))
    if path_prefix:
        query += " AND path LIKE ?"
        params.append(path_prefix + "%")
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    query += f" ORDER BY {order} LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return conn.execute(query, params).fetchall()


def count_symbols(conn: sqlite3.Connection, root: str,
                  name: Optional[str] = None,
                  path: Optional[str] = None,
                  kind: Optional[str] = None,
                  path_prefix: Optional[str] = None) -> int:
    """Total matching rows across ALL pages (same filters as
    query_symbols, without LIMIT/OFFSET). Feeds the query-response
    'total' field so a consumer can tell '22-row page of a 76-row
    set' (byte-capped page) from 'all 22 rows'. `path_prefix` = the
    L2.3b subtree arm (directory + os.sep, LIKE)."""
    query = "SELECT COUNT(*) FROM symbols WHERE root = ?"
    params: list = [root]
    if name:
        query += " AND name = ?"
        params.append(name)
    if path:
        query += " AND path = ?"
        params.append(os.path.abspath(path))
    if path_prefix:
        query += " AND path LIKE ?"
        params.append(path_prefix + "%")
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    return conn.execute(query, params).fetchone()[0]


def _concept_predicate(root: str, tokens: list,
                       path: Optional[str], kind: Optional[str]) -> tuple:
    """(sql_where_fragment, params) for a concept query.

    A name is a candidate when ANY token occurs as a word-surface slice of
    the symbol name (case-insensitive substring — the widest tier; the
    per-name tier / strong-hit count are computed separately in the
    SELECT, not the WHERE).

    L2.4d: UNCHANGED — the filtered leg (path/kind set) and the legacy
    symbols-scan leg keep this exact fragment (byte-identical output);
    the unfiltered leg pre-filters over name_vocab via
    _concept_vocab_predicate instead.
    """
    ors: list = []
    params: list = [root]
    for t in tokens:
        ors.append("lower(name) LIKE ?")
        params.append(f"%{t}%")
    frag = "root = ? AND (" + " OR ".join(ors) + ")"
    if path:
        frag += " AND path = ?"
        params.append(os.path.abspath(path))
    if kind:
        frag += " AND kind = ?"
        params.append(kind)
    return frag, params


def _concept_vocab_predicate(root: str, tokens: list) -> tuple:
    """L2.4d: the concept pre-filter fragment over name_vocab — the same
    per-token widest-net OR shape as _concept_predicate, evaluated on
    v.lname (already lower-case). Used ONLY when path/kind are None
    (the filtered leg cannot run on the vocabulary — it has no
    path/kind columns). Candidate set is byte-identical: a name passes
    the net on the vocabulary iff some symbol row passes it."""
    ors: list = []
    params: list = [root]
    for t in tokens:
        ors.append("v.lname LIKE ?")
        params.append(f"%{t}%")
    # v.root (NOT bare root): the leg JOINs name_vocab v against symbols s —
    # both carry a root column; bare `root` is an ambiguous-column error.
    return "v.root = ? AND (" + " OR ".join(ors) + ")", params


# L2.4b declaration-side kinds: a candidate row on one of these kinds is the
# SYMBOL ITSELF (where the thing is declared / what it is named after);
# every other kind (call_site / kwarg_usage / import / extends) is a USAGE
# of some name. Def-side candidates rank above usage candidates at equal
# rank keys (owner ruling 2026-11-17, T-B finding #4: implementation-side
# names must not lose to their own kwarg/call rows).
_DEF_SIDE_KINDS = ("function", "method", "class", "parameter", "decorator")
_DEF_SIDE_SQL = "kind IN (" + ", ".join("?" for _ in _DEF_SIDE_KINDS) + ")"


def _concept_rank_sql(tokens: list, where: str, wparams: list,
                      limit: int, offset: int, rarity: bool = True,
                      col: str = "lower(name)", vocab: bool = False) -> tuple:
    """L2.4b candidate SQL: DISTINCT-NAME ranking (aggregation only — no
    c0 re-JOIN; the representative row is fetched by query_concept's
    second, index-driven query).

    One candidate per distinct NAME (a 6-row call_site family is ONE
    candidate with occ=6 — the T-B drown fix). ORDER: strong hits DESC
    (a name that matches every concept word beats one that matches a
    single word), then tier ASC, then def-side before usage, then the
    SIDE-SPLIT 4th term (L2.4c): def-side candidates rank by occ DESC
    (cross-file heat — a production symbol used across many files is
    the answer a concept bridge should surface; the L2.7 locked
    retest proved the pre-2.4c `occ ASC` rarity term inverted this: a
    unique `test_*` def with occ=1 beat a 28-row production def),
    while usage-side-only candidates keep occ ASC (rarity — the
    original L2.4 accuracy guard, still correct on pure usage
    families), then name.

    `rarity=False` is the large-candidate-set path (gate ④,
    CONCEPT_RARITY_MAX_CANDIDATES): the per-name aggregates (occ / hits /
    defmark) are dropped — a plain GROUP BY over distinct names is far
    cheaper than the counted one, and the caller discloses the degraded
    ranking. Dedup-by-name is KEPT (that is the point of L2.4b even at
    scale).

    Tier-2 = DELIMITED strong match, built with GLOB for the underscore
    anchors: GLOB patterns treat '_' literally (a LIKE '_x%' would let
    the '_' wildcard swallow ANY leading character — 'xheal_y' must stay
    tier-3 for 'heal'). WHERE keeps the widest substring net.
    """
    tier_cases = []
    strong_flags = []
    tier_params: list = []
    strong_params: list = []
    # L2.4d: the tier/strong CASEs are template-bound to `col` —
    # "lower(name)" on the symbols-scan leg, "v.lname" on the
    # name_vocab pre-filter leg (byte-identical expressions otherwise).
    tier_tpl = (
        "(CASE WHEN {col} = ? THEN 1"
        " WHEN {col} LIKE ? OR {col} GLOB ?"
        " OR {col} GLOB ? THEN 2 ELSE 3 END)")
    strong_tpl = (
        "(CASE WHEN {col} = ? THEN 1"
        " WHEN {col} LIKE ? OR {col} GLOB ?"
        " OR {col} GLOB ? THEN 1 ELSE 0 END)")
    for t in tokens:
        tier_cases.append(tier_tpl.format(col=col))
        strong_flags.append(strong_tpl.format(col=col))
        for p in (t, t + "%", "_" + t + "*", "*_" + t):
            tier_params.append(p)
            strong_params.append(p)
    # SELECT list order: ALL tier CASEs first, then ALL strong-flag CASEs
    # (the params must follow that order — interleaving per token is the
    # binding shift that silently zeroed multi-token tiers)
    tparams = tier_params + strong_params
    least = (tier_cases[0] if len(tier_cases) == 1
             else "MIN(" + ", ".join(tier_cases) + ")")
    # single-token: ((flag)) is a plain expression — safe; MIN((x)) would
    # be a single-arg AGGREGATE that collapses a bare SELECT to one row
    if rarity:
        hits_sum = (((" + ".join(strong_flags))
                     if len(strong_flags) > 1
                     else strong_flags[0]) + " AS hits")
        ranked_core = (
            " MIN(c.tier) AS tier, MIN(c.hits) AS hits, COUNT(*) AS occ,"
            f" MIN(CASE WHEN {_DEF_SIDE_SQL}"
            " THEN 0 ELSE 1 END) AS defmark, MIN(c.line) AS minline")
        order = ("ORDER BY hits DESC, tier, defmark,"
                 " CASE WHEN defmark = 0 THEN -occ ELSE occ END, ln")
        tail = (" SELECT root, name, ln, tier, hits, occ, defmark, minline"
                " FROM ranked " + order + " LIMIT ? OFFSET ?")
        ranked_params = (tparams + wparams + list(_DEF_SIDE_KINDS)
                         + [limit, offset])
    else:
        # gate-④ degraded path (rarity=False): no per-name aggregates
        # (the counted GROUP BY is the 100ms+ cost at 20k+ candidates);
        # dedup by name stays. NOTE: c0 carries the tier CASEs only —
        # the strong-flag CASEs are absent, so the strong params must
        # NOT be supplied (binding shift = ProgrammingError).
        hits_sum = "0 AS hits"
        ranked_core = (
            " MIN(c.tier) AS tier, 0 AS hits, 0 AS occ, 1 AS defmark,"
            " MIN(c.line) AS minline")
        order = "ORDER BY tier, ln"
        tail = (" SELECT root, name, ln, tier, hits, occ, defmark, minline"
                " FROM ranked " + order + " LIMIT ? OFFSET ?")
        ranked_params = tier_params + wparams + [limit, offset]
    if vocab:
        # L2.4d fast leg: pre-filter the tier-3 net over name_vocab
        # (O(distinct names)), JOIN back to symbols via the lower(name)
        # expression index — the candidate SET is byte-identical to the
        # symbols scan (the caller only takes this branch with
        # path/kind None; the WHERE is _concept_vocab_predicate).
        from_clause = (
            "FROM name_vocab v JOIN symbols s"
            " ON s.root = v.root AND lower(s.name) = v.lname"
            f" WHERE {where}")
    else:
        from_clause = f"FROM symbols s WHERE {where}"
    ranked_sql = (
        "WITH c0 AS ("
        " SELECT s.root, s.path, s.name, s.kind, s.line, s.sig,"
        f" {least} AS tier, {hits_sum}"
        f" {from_clause}"
        "), ranked AS ("
        " SELECT c.root, lower(c.name) AS ln, c.name AS name,"
        + ranked_core
        + " FROM c0 c GROUP BY c.root, lower(c.name)"
        ")"
        + tail
    )
    return ranked_sql, ranked_params


def _concept_rep_sql(root: str, lns: list) -> tuple:
    """Second query of query_concept: the REPRESENTATIVE rows for the
    ranked names — a single IN over the lower(name) EXPRESSION index
    (idx_symbols_lname), so each name is an index probe. (An OR chain of
    `(lower(name)=? AND line=?)` pairs is NOT sargable as a whole:
    SQLite full-scans the table — measured 913ms at 20k rows vs tens of
    ms for the IN.) The caller picks each name's min-line row in
    Python. Match on lower(name) (the GROUP BY key) — the ranked CTE's
    bare `name` column carries an arbitrary-case value and must not
    drive the lookup."""
    if not lns:
        return None, None
    placeholders = ", ".join("?" for _ in lns)
    sql = (f"SELECT name, kind, path, line, sig FROM symbols"
           f" WHERE root = ? AND lower(name) IN ({placeholders})")
    return sql, [root, *lns]


def query_concept(conn: sqlite3.Connection, root: str, tokens: list,
                  path: Optional[str] = None, kind: Optional[str] = None,
                  limit: int = 200, offset: int = 0,
                  rarity: bool = True) -> list:
    """L2.4b concept bridge: DISTINCT-NAME word-surface candidates,
    ranked (strong-hits DESC, tier ASC, def-side first, rarity ASC,
    stable keys).

    L2.4b (owner 2026-11-17) replaced the row-level candidate list: a
    multi-row usage family no longer drowns the one distinct name that
    carries the concept; tiers promote leading-underscore prefixes and
    underscore-delimited suffixes; with 2+ concept tokens a name that
    strongly matches every token outranks single-token names; within a
    rank, declaration rows (function/method/class/parameter/decorator)
    outrank usage rows.

    `rarity=False` is gate ④ (CONCEPT_RARITY_MAX_CANDIDATES): the
    per-name aggregates (occ / hits / defmark) are placeholders and the
    ranking degrades to (tier, name) — dedup-by-name stays; the caller
    discloses the degraded payload.

    L2.4d: with path/kind both None the pre-filter runs over the
    distinct-name vocabulary (name_vocab, O(distinct names)) instead of
    the whole symbols table — the candidate set is byte-identical;
    filtered queries keep the symbols-scan leg.

    Rows: (root, path, name, kind, line, sig, tier, occ, hits, defmark).
    """
    if path is None and kind is None:
        where, wparams = _concept_vocab_predicate(root, tokens)
        ranked_sql, ranked_params = _concept_rank_sql(
            tokens, where, wparams, limit, offset, rarity,
            col="v.lname", vocab=True)
    else:
        where, wparams = _concept_predicate(root, tokens, path, kind)
        ranked_sql, ranked_params = _concept_rank_sql(
            tokens, where, wparams, limit, offset, rarity)
    ranked = conn.execute(ranked_sql, ranked_params).fetchall()
    # (root, name, ln, tier, hits, occ, defmark, minline)
    if not ranked:
        return []
    rep_sql, rep_params = _concept_rep_sql(root, [r[2] for r in ranked])
    reps = {}
    for rr in conn.execute(rep_sql, rep_params).fetchall():
        # (name, kind, path, line, sig) — key on (lower-name, line) so a
        # name's many rows stay distinguishable; the min-line row wins
        reps[(rr[0].lower(), rr[3])] = rr
    out = []
    for r in ranked:
        root_v, _name, ln, tier, hits, occ, defmark, minline = r
        rep = reps.get((ln, minline))
        if rep is None:
            continue  # the min-line row left the index mid-query
        out.append((root_v, rep[2], rep[0], rep[1], rep[3], rep[4],
                    tier, occ, hits, defmark))
    return out


def count_concept(conn: sqlite3.Connection, root: str, tokens: list,
                  path: Optional[str] = None, kind: Optional[str] = None) -> int:
    """All-candidate DISTINCT-NAME count for a concept query (feeds
    'total' — L2.4b: total is names, not rows).

    L2.4d: unfiltered (path/kind None) counts run over name_vocab
    (one row per distinct name — the count IS the vocabulary size of the
    net-matching names); filtered counts stay on the symbols scan."""
    if path is None and kind is None:
        where, params = _concept_vocab_predicate(root, tokens)
        q = f"SELECT COUNT(*) FROM name_vocab v WHERE {where}"
        return conn.execute(q, params).fetchone()[0]
    where, params = _concept_predicate(root, tokens, path, kind)
    q = f"SELECT COUNT(DISTINCT lower(name)) FROM symbols WHERE {where}"
    return conn.execute(q, params).fetchone()[0]


def stale_paths(conn: sqlite3.Connection, root: str,
                on_disk) -> set:
    """Return paths in the index that are no longer on disk."""
    rows = conn.execute(
        "SELECT path FROM files WHERE path LIKE ?",
        (f"{root}{os.sep}%",),
    ).fetchall()
    indexed = {r[0] for r in rows}
    return indexed - set(on_disk)


def remove_paths(conn: sqlite3.Connection, paths: set,
                 commit: bool = True, prune_vocab: bool = True) -> None:
    for p in paths:
        conn.execute("DELETE FROM symbols WHERE path = ?", (p,))
        conn.execute("DELETE FROM files WHERE path = ?", (p,))
    # L2.4d prune (reviewer finding F1, 2026-11-18): vocab rows whose
    # names lost their LAST symbol row become orphans — harmless to the
    # ranked leg (JOIN-excluded) but they inflate count_concept's total.
    # Prune at the API boundary (the heal path for vanished files).
    # L2.8j (2026-11-18): the prune is a SINGLE-PASS set op
    # (prune_orphan_vocab — the old correlated NOT EXISTS planned as
    # O(vocab x root_symbols), ~15 s/file at the live 32k-row scale) and
    # callers that delete many files in one build pass prune_vocab=False
    # and prune ONCE after the loop (amortization — the L2.3c
    # generation-mismatch re-parse loop used to pay it per file).
    if prune_vocab:
        prune_orphan_vocab(conn)
    if commit:
        conn.commit()


def prune_orphan_vocab(conn: sqlite3.Connection) -> None:
    """Drop name_vocab rows whose name lost its LAST symbol row.

    L2.8j: single-pass index-driven set op — the surviving-name set is
    ONE DISTINCT projection over symbols (idx_symbols_lname-driven,
    streamed) materialized once; the 3k-row vocab table is then a plain
    membership scan. O(root_rows + vocab), NOT the old
    O(vocab x root_symbols) correlated plan (15 s/file measured at the
    live scale). No commit — the caller owns the transaction.
    """
    conn.execute(
        "DELETE FROM name_vocab WHERE lname NOT IN"
        " (SELECT DISTINCT lower(name) FROM symbols)")


def gen_stale_paths(conn: sqlite3.Connection, root: str) -> set:
    """L2.8i: indexed files stamped under an OLDER parser generation.

    The caller intersects with on_disk (a gen-stale file that was also
    deleted is handled by the disk-missing path, not the re-parse set).
    Legacy '' rows (pre-L2.3c, unknown generation) are re-parsed by
    design, so `!= PARSER_GENERATION` is the right shape.

    Separator-normalization: legacy rows were stored with the path
    separator of the ERA that minted them (forward-slash kernel roots
    vs backslash registry roots), so a LIKE prefix over the raw root
    string misses cross-era rows — they would sit gen-stale forever
    (the code_map target's 4 files, found 2026-11-18). Match on the
    normalized form (os.sep in the query value, '/' in the stored value)
    so both separator families are covered.
    """
    norm = lambda p: p.replace("/", os.sep).replace(os.sep, "/")
    norm_root = root.replace("/", os.sep).replace(os.sep, "/")
    # LIKE in BOTH separator families (stored-era keys can be either;
    # the Python-side prefix filter below enforces exactness — LIKE is
    # only the narrowing leg).
    rows = conn.execute(
        "SELECT path FROM files WHERE (path LIKE ? OR path LIKE ?)"
        " AND parser_gen != ?",
        ("%" + norm_root + "%",
         "%" + norm_root.replace("/", os.sep) + "%",
         PARSER_GENERATION)).fetchall()
    return {r[0] for r in rows if norm(r[0]).startswith(norm_root + "/")}
