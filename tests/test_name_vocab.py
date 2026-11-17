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
