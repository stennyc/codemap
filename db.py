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

from .config import root_db_path, WAL_BUSY_TIMEOUT_MS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path          TEXT PRIMARY KEY,
    mtime         REAL NOT NULL,
    size          INTEGER NOT NULL,
    lang          TEXT NOT NULL,
    build_id      TEXT NOT NULL,
    fingerprint_epoch INTEGER NOT NULL DEFAULT 0
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
    conn.commit()
    return conn


def upsert_file(conn: sqlite3.Connection, path: str, mtime: float,
                size: int, lang: str, build_id: str,
                commit: bool = True) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO files(path, mtime, size, lang, build_id, fingerprint_epoch)"
        " VALUES(?, ?, ?, ?, ?, 0)",
        (path, mtime, size, lang, build_id),
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
    if commit:
        conn.commit()


def query_symbols(conn: sqlite3.Connection, root: str,
                  name: Optional[str] = None,
                  path: Optional[str] = None,
                  kind: Optional[str] = None,
                  limit: int = 200,
                  offset: int = 0) -> list:
    """Query symbol rows for `root`. `path` must be an ABSOLUTE file path —
    it matches EXACTLY one indexed file (the symbols table stores absolute
    paths, see insert_symbols / _reindex_single_file_sync). A relative or
    suffix-style path used to under-match or over-match sibling files with
    the same basename (the b-1 attach contamination: `__init__.py` pulled
    in every subpackage's __init__ symbols); exact match removes both.
    """
    query = "SELECT root, path, name, kind, line, sig FROM symbols WHERE root = ?"
    params: list = [root]
    if name:
        query += " AND name = ?"
        params.append(name)
    if path:
        query += " AND path = ?"
        params.append(os.path.abspath(path))
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    query += " ORDER BY line LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return conn.execute(query, params).fetchall()


def count_symbols(conn: sqlite3.Connection, root: str,
                  name: Optional[str] = None,
                  path: Optional[str] = None,
                  kind: Optional[str] = None) -> int:
    """Total matching rows across ALL pages (same filters as
    query_symbols, without LIMIT/OFFSET). Feeds the query-response
    'total' field so a consumer can tell '22-row page of a 76-row
    set' (byte-capped page) from 'all 22 rows'.
    """
    query = "SELECT COUNT(*) FROM symbols WHERE root = ?"
    params: list = [root]
    if name:
        query += " AND name = ?"
        params.append(name)
    if path:
        query += " AND path = ?"
        params.append(os.path.abspath(path))
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    return conn.execute(query, params).fetchone()[0]


def _concept_predicate(root: str, tokens: list,
                       path: Optional[str], kind: Optional[str]) -> tuple:
    """(sql_where_fragment, params) for a concept query.

    A row is a candidate when ANY token occurs as a word-surface slice of
    the symbol name (case-insensitive substring — the widest tier; the
    per-row tier is computed separately in the SELECT, not the WHERE).
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


def _tier_exprs(tokens: list) -> tuple:
    """Per-token tier CASE exprs for a MIN(): 1=name-equal, 2=prefix,
    3=substring (a row reaches this query only via the substring OR).

    NOTE: the joined exprs must NOT be wrapped in one outer paren pair —
    MIN((a, b)) is a row-value arg (sqlite 'row value misused');
    MIN(a, b) is the multi-arg min we want."""
    exprs = []
    params: list = []
    for t in tokens:
        exprs.append(
            f"(CASE WHEN lower(name) = ? THEN 1"
            f" WHEN lower(name) LIKE ? THEN 2 ELSE 3 END)")
        params.extend([t, t + "%"])
    return ", ".join(exprs), params


def query_concept(conn: sqlite3.Connection, root: str, tokens: list,
                  path: Optional[str] = None, kind: Optional[str] = None,
                  limit: int = 200, offset: int = 0,
                  rarity: bool = True) -> list:
    """L2.4 concept bridge (owner 2026-11-16): word-surface candidate
    rows ranked (tier ASC, name-rarity ASC, then stable keys).

    Rarity = how many rows in THIS root share the candidate's exact name
    (GROUP BY pass): a rare name like `rebuild_dirty` outranks a common
    one like `build` for the same token — the accuracy guard against
    top-1 over-trust. When rarity=False (large candidate sets, gate ④
    budget) the occ column is 0 and ranking falls back to
    (tier, name, path, line) — the caller picks via candidate total.
    Rows: (root, path, name, kind, line, sig, tier, occ).
    """
    where, wparams = _concept_predicate(root, tokens, path, kind)
    tier_sql, tparams = _tier_exprs(tokens)
    if rarity:
        query = (
            "SELECT s.root, s.path, s.name, s.kind, s.line, s.sig,"
            f" MIN({tier_sql}) AS tier, r.occ AS occ"
            " FROM symbols s"
            " JOIN (SELECT lower(name) ln, COUNT(*) occ FROM symbols"
            "       WHERE root = ? GROUP BY 1) r ON r.ln = lower(s.name)"
            f" WHERE {where}"
            " GROUP BY s.root, s.path, s.name, s.kind, s.line, s.sig, r.occ"
            " ORDER BY tier, occ, s.name, s.path, s.line"
            " LIMIT ? OFFSET ?"
        )
        # SQLite binds placeholders in SQL-TEXT order:
        #   1. SELECT: tier CASE exprs (tparams: per token -> tok, tok%)
        #   2. FROM subquery: the join root
        #   3. WHERE fragment: wparams (leading root + LIKE %tok% + path/kind)
        #   4. LIMIT / OFFSET
        params = tparams + [root] + wparams + [limit, offset]
    else:
        # No-rarity fast path: plain tier-ordered scan (no GROUP BY JOIN —
        # the rarity join is O(root size) and would blow the 50ms gate on
        # 30k-row roots). occ is 0; callers disclose the fallback ranking.
        query = (
            "SELECT s.root, s.path, s.name, s.kind, s.line, s.sig,"
            f" MIN({tier_sql}) AS tier, 0 AS occ"
            f" FROM symbols s WHERE {where}"
            " ORDER BY tier, s.name, s.path, s.line"
            " LIMIT ? OFFSET ?"
        )
        params = tparams + wparams + [limit, offset]
    return conn.execute(query, params).fetchall()


def count_concept(conn: sqlite3.Connection, root: str, tokens: list,
                  path: Optional[str] = None, kind: Optional[str] = None) -> int:
    """All-page candidate count for a concept query (feeds 'total')."""
    where, params = _concept_predicate(root, tokens, path, kind)
    q = f"SELECT COUNT(*) FROM symbols WHERE {where}"
    return conn.execute(q, params).fetchone()[0]


def stale_paths(conn: sqlite3.Connection, root: str,
                on_disk) -> set:
    """Return paths in the index that are no longer on disk."""
    rows = conn.execute(
        "SELECT path FROM files WHERE path LIKE ?",
        (f"{root}/%",),
    ).fetchall()
    indexed = {r[0] for r in rows}
    return indexed - set(on_disk)


def remove_paths(conn: sqlite3.Connection, paths: set,
                 commit: bool = True) -> None:
    for p in paths:
        conn.execute("DELETE FROM symbols WHERE path = ?", (p,))
        conn.execute("DELETE FROM files WHERE path = ?", (p,))
    if commit:
        conn.commit()
