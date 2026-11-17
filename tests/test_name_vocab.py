"""L2.4d: the concept-bridge pre-filter runs over the distinct-name
vocabulary table (name_vocab), not the 62k-row symbols table.

Measured before L2.4d (filemem live db, 61,761 rows, 2,990 distinct
names): count_concept 125ms + query_concept 281ms = idle p50 214ms /
max 303ms — OUTSIDE the 150ms concept gate with NO load at all (table
growth 35k→62k since the L2.7.1 warm measurement). Both cost centers
scan every symbol row to evaluate `lower(name) LIKE '%token%'`; the
vocabulary table makes the pre-filter O(distinct-names) and the
aggregate O(matched-row-families). The candidate SET is byte-identical
(vocab is an exact projection: a name passes the tier-3 net on the
vocabulary iff some row passes it — the per-name tier/hits/occ
aggregates are computed over the same row families via the JOIN).

Invariants this file pins:
- init_db backfills a legacy db (symbols without vocab rows);
- insert_symbols maintains vocab (idempotent INSERT OR IGNORE);
- the no-path/no-kind fuzzy path EXPLAINs over name_vocab (the fast
  leg) while path/kind-filtered queries stay on the symbols-scan leg;
- results are identical on both legs (a stale vocab row with zero
  symbol rows is JOIN-excluded — harmless, documented);
- _concept_predicate's output stays byte-identical (zero regression
  on the symbols path).
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_imports() -> None:
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)


def _seed_root(monkeypatch, tmp_path: Path, use_insert_symbols=True) -> str:
    """Isolated root: 4 files, 'widget'/'helper' concept families (same
    shape as test_concept_ranking_heat) — plus a BULK 8,000-row
    call_site family so the EXPLAIN check is meaningful at scale."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _fresh_imports()
    from code_map import db as cdb
    from code_map import rootreg, config

    proj = tmp_path / "vocabproj"
    proj.mkdir()
    rels = ["a.py", "b.py", "c.py", "t.py"]
    for rel in rels:
        (proj / rel).write_text("x = 1\n", encoding="utf-8")
    root = str(proj)
    rootreg.write_confirmed_root(root)

    rows = {
        "a.py": [
            ("widget_service", "function", 1, "def widget_service():"),
            ("widget_service", "call_site", 5, "widget_service()"),
            ("rare_helper", "call_site", 9, "rare_helper()"),
        ],
        "b.py": [
            ("widget_service", "call_site", 1, "widget_service()"),
            ("hot_helper", "call_site", 2, "hot_helper()"),
        ],
        "c.py": [
            ("_widget_probe", "function", 1, "def _widget_probe():"),
            ("hot_helper", "call_site", 2, "hot_helper()"),
        ],
        "t.py": [
            ("hot_helper", "call_site", 5, "hot_helper()"),
        ],
    }
    conn = cdb.init_db(root)
    try:
        for rel, sym_rows in rows.items():
            p = Path(root) / rel
            st = os.stat(str(p))
            cdb.upsert_file(conn, str(p), st.st_mtime, st.st_size, "python",
                            "t", parser_gen=config.PARSER_GENERATION)
            cdb.insert_symbols(conn, root, str(p), sym_rows, commit=False)
        # bulk family: 8,000 call_site rows under one name (scale shape)
        bulk = [("bulk_worker", "call_site", i, "bulk_worker()")
                for i in range(2, 8002, 2)]
        cdb.insert_symbols(conn, root, str(Path(root) / "b.py"), bulk,
                           commit=False)
        conn.commit()
    finally:
        conn.close()
    return root


def test_vocab_backfilled_on_open(monkeypatch, tmp_path):
    """A LEGACY db (symbols rows inserted raw, no vocab maintenance)
    gets name_vocab populated on the next init_db open — one-shot,
    idempotent."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _fresh_imports()
    from code_map import db as cdb
    from code_map import config
    root = str(tmp_path / "legacyroot")
    Path(root).mkdir()
    p = Path(root) / "f.py"
    p.write_text("def legacy():\n    pass\n", encoding="utf-8")
    conn = cdb.init_db(root)
    try:
        # raw insert = the legacy shape (pre-L2.4d write path had no vocab)
        conn.execute("INSERT INTO files(path, mtime, size, lang, build_id,"
                     " fingerprint_epoch, parser_gen) VALUES(?,?,?,?,?,0,'')",
                     (str(p), 0, 1, "python", "t"))
        conn.execute("INSERT INTO symbols(name, kind, line, sig, path, root)"
                     " VALUES('legacy','function',1,'def legacy():',?,?)",
                     (str(p), root))
        conn.commit()
    finally:
        conn.close()
    # reopen: the backfill gate must have run
    conn = cdb.init_db(root)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT lname FROM name_vocab WHERE root=?", (root,))]
        assert names == ["legacy"], names
    finally:
        conn.close()


def test_insert_symbols_maintains_vocab_idempotent(monkeypatch, tmp_path):
    root = _seed_root(monkeypatch, tmp_path)
    _fresh_imports()
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT lname FROM name_vocab WHERE root=?", (root,))]
        assert set(names) == {"widget_service", "rare_helper", "hot_helper",
                              "_widget_probe", "bulk_worker"}, names
        # idempotency: re-insert the same rows → still one row per name
        cdb.insert_symbols(conn, root, str(Path(root) / "a.py"),
                           [("widget_service", "function", 1,
                             "def widget_service():")])
        n = conn.execute(
            "SELECT COUNT(*) FROM name_vocab WHERE root=? AND lname='widget_service'",
            (root,)).fetchone()[0]
        assert n == 1, n
    finally:
        conn.close()


def test_fuzzy_no_filter_scans_vocab_not_symbols(monkeypatch, tmp_path):
    """The fast leg is PROVEN STRUCTURALLY (EXPLAIN), not by wall time
    (the suite's load-flake discipline): no path/kind → the concept
    pre-filter drives off name_vocab; path/kind filters stay on the
    symbols-scan leg (vocab has no path/kind columns)."""
    root = _seed_root(monkeypatch, tmp_path)
    _fresh_imports()
    from code_map import db as cdb

    conn = cdb.init_db(root)
    try:
        where, wparams = cdb._concept_vocab_predicate(root, ["widget"])
        sel, sp = cdb._concept_rank_sql(["widget"], where, wparams, 200, 0,
                                        rarity=True, col="v.lname", vocab=True)
        plan = " ".join(r[-1] for r in conn.execute(
            "EXPLAIN QUERY PLAN " + sel, sp).fetchall())
        assert "name_vocab" in plan, plan
        # the filtered leg keeps the symbols scan
        where_s, wparams_s = cdb._concept_predicate(root, ["widget"], None,
                                                     None)
        sel2, sp2 = cdb._concept_rank_sql(["widget"], where_s, wparams_s,
                                          200, 0, rarity=True, vocab=False)
        plan2 = " ".join(r[-1] for r in conn.execute(
            "EXPLAIN QUERY PLAN " + sel2, sp2).fetchall())
        assert "name_vocab" not in plan2, plan2
    finally:
        conn.close()


def test_vocab_leg_results_identical_to_symbols_leg(monkeypatch, tmp_path):
    """Correctness pin: the two legs must return byte-identical
    candidate sets (same names, tiers, occ, order)."""
    root = _seed_root(monkeypatch, tmp_path)
    _fresh_imports()
    from code_map import db as cdb

    conn = cdb.init_db(root)
    try:
        where, wparams = cdb._concept_vocab_predicate(root, ["widget"])
        fast, fastp = cdb._concept_rank_sql(["widget"], where, wparams,
                                             200, 0, rarity=True,
                                             col="v.lname", vocab=True)
        where_s, wparams_s = cdb._concept_predicate(root, ["widget"], None,
                                                     None)
        slow, slowp = cdb._concept_rank_sql(["widget"], where_s, wparams_s,
                                            200, 0, rarity=True,
                                            vocab=False)
        a = conn.execute(fast, fastp).fetchall()
        b = conn.execute(slow, slowp).fetchall()
        assert a == b, (a, b)
        # bulk-word check on the hot family (heat ordering preserved)
        where2, wparams2 = cdb._concept_vocab_predicate(root, ["bulk"])
        f2, fp2 = cdb._concept_rank_sql(["bulk"], where2, wparams2, 200, 0,
                                        rarity=True, col="v.lname",
                                        vocab=True)
        r2 = conn.execute(f2, fp2).fetchall()
        assert len(r2) == 1 and r2[0][2] == "bulk_worker", r2
    finally:
        conn.close()


def test_stale_vocab_row_is_join_harmless(monkeypatch, tmp_path):
    """A full-rescan that drops a name's symbol rows leaves its vocab
    row behind (bounded, documented): the JOIN excludes it from results
    — no phantom candidate, no error; re-indexing restores it."""
    root = _seed_root(monkeypatch, tmp_path)
    _fresh_imports()
    from code_map import db as cdb

    conn = cdb.init_db(root)
    try:
        conn.execute("DELETE FROM symbols WHERE root=? AND name='rare_helper'",
                     (root,))
        conn.commit()
        where, wparams = cdb._concept_vocab_predicate(root, ["rare"])
        sel, sp = cdb._concept_rank_sql(["rare"], where, wparams, 200, 0,
                                        rarity=True, col="v.lname",
                                        vocab=True)
        rows = conn.execute(sel, sp).fetchall()
        assert rows == [], rows  # the orphaned vocab row produced NO candidate
        # restore: re-index the file's rows → the name is back
        cdb.insert_symbols(conn, root, str(Path(root) / "a.py"),
                           [("rare_helper", "call_site", 9, "rare_helper()")])
        rows = conn.execute(sel, sp).fetchall()
        assert len(rows) == 1 and rows[0][2] == "rare_helper", rows
    finally:
        conn.close()


def test_concept_predicate_byte_identical(monkeypatch, tmp_path):
    """_concept_predicate is UNCHANGED by L2.4d (the symbols leg keeps
    its exact fragment + params): pin the emitted shape."""
    _fresh_imports()
    from code_map import db as cdb

    frag, params = cdb._concept_predicate("/root", ["alpha", "beta"],
                                          None, None)
    assert frag == ("root = ? AND (lower(name) LIKE ? OR lower(name) LIKE ?)"), frag
    assert params == ["/root", "%alpha%", "%beta%"], params
    frag2, params2 = cdb._concept_predicate("/root", ["alpha"], "/p.py", "kind")
    assert frag2 == ("root = ? AND (lower(name) LIKE ?) AND path = ?"
                     " AND kind = ?"), frag2
    assert params2 == ["/root", "%alpha%", os.path.abspath("/p.py"),
                       "kind"], params2


def test_root_lname_index_created_on_open(monkeypatch, tmp_path):
    """The L2.4d composite index (root, lower(name)) must exist on ANY
    opened db (legacy backfill via CREATE INDEX IF NOT EXISTS in
    _SCHEMA) — it is what turns the concept leg's name probes into
    per-name index ranges instead of a whole-root walk."""
    root = _seed_root(monkeypatch, tmp_path)
    _fresh_imports()
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        idx = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='symbols'")]
        assert "idx_symbols_root_lname" in idx, idx
    finally:
        conn.close()


# ── reviewer-findings follow-ups (2026-11-18 复盘 F1/F2/F3) ────────────────

def test_remove_paths_prunes_vocab(monkeypatch, tmp_path):
    """F1: a file removal via the REAL API (remove_paths — the heal path
    for vanished source files) must prune the vocab rows whose names no
    longer have ANY symbol row, so count_concept's total stays exact.
    (A raw DELETE of symbol rows still leaves orphans by design — the
    JOIN-exclusion test above; the prune rides on the API boundary.)"""
    root = _seed_root(monkeypatch, tmp_path)
    _fresh_imports()
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        a = str(Path(root) / "a.py")
        # rare_helper exists ONLY in a.py → its vocab row must die
        assert cdb.count_concept(conn, root, ["rare"]) == 1
        cdb.remove_paths(conn, {a})
        assert cdb.count_concept(conn, root, ["rare"]) == 0, \
            "orphaned vocab row inflates total after the file is gone"
        # names still living in other files survive the prune
        assert cdb.count_concept(conn, root, ["widget"]) >= 1
        assert cdb.count_concept(conn, root, ["hot"]) == 1
    finally:
        conn.close()


def test_degraded_vocab_binding_invariant(monkeypatch, tmp_path):
    """F3: the degraded (rarity=False) + vocab leg is a distinct SQL
    branch with a distinct parameter list (tier CASEs only — the
    strong-flag CASEs and the def-side kinds are absent). The branch's
    classic failure mode is a binding shift (ProgrammingError or a
    silent parameter drift): pin count('?') == len(params) AND a clean
    execution with the placeholder aggregates."""
    root = _seed_root(monkeypatch, tmp_path)
    _fresh_imports()
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        where, wparams = cdb._concept_vocab_predicate(root, ["widget"])
        sel, sp = cdb._concept_rank_sql(["widget"], where, wparams, 200, 0,
                                        rarity=False, col="v.lname",
                                        vocab=True)
        assert sel.count("?") == len(sp), \
            f"binding shift on the degraded+vocab branch: {sel.count('?')} vs {len(sp)}"
        rows = conn.execute(sel, sp).fetchall()
        assert rows, "the degraded leg must still return candidates"
        # placeholder aggregates: hits=0, occ=0, defmark=1 (usage-shaped)
        assert all(r[5] == 0 and r[6] == 1 for r in rows), rows
    finally:
        conn.close()


def test_fuzzy_param_doc_carries_l24c_split(monkeypatch, tmp_path):
    """F2: the `fuzzy` PARAMETER-level schema description (the face the
    model reads via tool_describe) must carry the L2.4c side-split
    contract — the pre-L2.4c wording pinned 'defside->rarity' for ALL
    candidates, which inverts on the def-side. Pin the NEW marker so a
    future rewrite cannot silently regress to the stale contract."""
    _fresh_imports()
    import code_map
    doc = code_map._TOOL_SCHEMAS["code_map_query"]["parameters"]["properties"][
        "fuzzy"]["description"]
    assert "heat" in doc, doc  # the def-side term must be visible
    assert "defside->rarity" not in doc, doc  # the stale flat-rarity wording is gone


# ── Recall-surface sync pins (L2.8i era, 2026-11-18) ──────────────────────

def test_staleness_doc_names_three_values_and_refresh_heal():
    """The model-facing query description must name ALL THREE staleness
    values (the pre-sync face knew only 'partial' — the model would meet
    parser_gen_stale stamps without a legend) and state the L2.8i
    remedy semantics (refresh re-parses + re-stamps the drift, so the
    flag heals on refresh). Same F2 discipline: read the LIVE schema
    surface so a rewrite cannot silently regress the contract."""
    _fresh_imports()
    import code_map
    qdoc = code_map._TOOL_SCHEMAS["code_map_query"]["description"]
    for val in ("'fresh'", "'partial'", "'parser_gen_stale'"):
        assert val in qdoc, f"staleness value {val} missing from the query face"
    assert "parser_gen_stale" in qdoc and "heal" in qdoc.lower(), \
        "the refresh-remedy semantics must be visible on the query face"
    assert "force a fresh re-scan" not in qdoc, \
        "the stale two-value-era wording must be gone"
    rdoc = code_map._TOOL_SCHEMAS["code_map_refresh"]["description"]
    assert "parser-generation drift" in rdoc, \
        "the refresh face must state what it ALSO clears (L2.8i)"
    assert "re-stamped" in rdoc


def test_root_policy_legend_names_parser_gen_stale():
    """The frozen root-policy legend (injected at session start) must
    carry the three-valued stamp legend — the pre-sync ellipsis
    'fresh|partial|...' left the third value unexplained."""
    _fresh_imports()
    import code_map
    legend = code_map._ROOT_POLICY_TEXT
    assert "parser_gen_stale" in legend, \
        "the legend must name the third staleness value"
    assert "fresh|partial|..." not in legend, \
        "the ellipsis form must be gone (all values spelled out)"
