"""W1 pins (2026-11-17): the exact branch's O(log) covering probe.

780c58a shipped two halves of the W1 fix; this file pins BOTH so a future
DDL drop or measurement-window regression is caught here, not on the live
32k-row host:

- (a) t0 starts AFTER the staleness fingerprint walk -> query_ms measures
  the query layer only (the walk still runs; staleness reporting intact).
  Pinned by monkey-slowing on_disk_files and asserting query_ms stays in
  the 50ms gate while the payload still carries `staleness`.
- (b) `idx_symbols_root_name_kind ON symbols(root, name, kind)` drives
  the equality probe (root=? AND name=? AND kind=?). Pinned by EXPLAIN
  (plan must name the index, not the root-prefix of idx_symbols_root_lname
  = the 32k-row full-root scan that W1 diagnosed) + a 35k-row steady-state
  gate check (min-of-5 query_ms <= 50, same gate-object discipline as
  test_kind_aggregation::test_kind_scan_gate_at_35k_rows).
- DDL guard: the CREATE INDEX line must stay in db._SCHEMA (idempotent
  backfill on every init_db open — dropping the line is the silent way the
  plan regresses).
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_imports() -> None:
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)


def _seed_exact_fixture(tmp_path, monkeypatch) -> str:
    """HERMES_HOME-isolated confirmed root with a 35k-row single-file db."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _fresh_imports()
    from code_map import db as cdb
    from code_map import rootreg

    proj = tmp_path / "exactpin"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")
    root = str(proj)
    rootreg.write_confirmed_root(root)

    conn = cdb.init_db(root)
    try:
        rows = [(f"n{i}", "function", i + 1, f"def n{i}():") for i in range(35000)]
        cdb.insert_symbols(conn, root, str(proj / "a.py"), rows, commit=True)
    finally:
        conn.close()
    return root


def _q(root, **kw):
    from code_map import query
    out = query.code_map_query({"root": root, **kw})
    return json.loads(out)


def test_ddl_present_in_schema(tmp_path, monkeypatch):
    """The covering composite must stay in _SCHEMA (silent-drop guard)."""
    _seed_exact_fixture(tmp_path, monkeypatch)
    from code_map import db as cdb
    assert "idx_symbols_root_name_kind" in cdb._SCHEMA


def test_exact_plan_uses_covering_index(tmp_path, monkeypatch):
    """EXPLAIN: the equality probe plans onto the new composite, NOT the
    root-prefix of idx_symbols_root_lname (the 32k-row scan W1 found)."""
    root = _seed_exact_fixture(tmp_path, monkeypatch)
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        sql = ("SELECT root, path, name, kind, line, sig FROM symbols "
               "WHERE root = ? AND name = ? AND kind = ? "
               "ORDER BY line LIMIT 25 OFFSET 0")
        plan = " ".join(r[-1] for r in conn.execute(
            "EXPLAIN QUERY PLAN " + sql, (root, "n12345", "function")).fetchall())
        assert "idx_symbols_root_name_kind" in plan, (
            f"plan regressed to a non-covering path: {plan}")
        assert "root_lname" not in plan
    finally:
        conn.close()


def test_query_ms_excludes_staleness_walk(tmp_path, monkeypatch):
    """W1(a): slowing the fingerprint walk 80ms must NOT inflate the gated
    query_ms (t0 starts after the walk) while staleness reporting stays."""
    root = _seed_exact_fixture(tmp_path, monkeypatch)
    import code_map.query as qmod

    def slow_walk(*a, **k):
        time.sleep(0.08)
        return []

    monkeypatch.setattr(qmod, "on_disk_files", slow_walk)
    out = _q(root, name="n12345", kind="function")
    assert out.get("staleness") is not None, "walk must still run"
    assert out["query_ms"] <= 50, (
        f"walk leaked back into the gated window: {out['query_ms']}ms")


def test_exact_gate_at_35k_rows(tmp_path, monkeypatch):
    """Steady-state gate-object check at live filemem scale (min-of-5,
    warm-up uncounted — same discipline as the 35k kind-scan gate)."""
    root = _seed_exact_fixture(tmp_path, monkeypatch)
    out = _q(root, name="n9999", kind="function")   # warm-up, uncounted
    best = float("inf")
    for _ in range(5):
        out = _q(root, name="n9999", kind="function")
        best = min(best, out["query_ms"])
    assert best <= 50, f"exact name+kind {best}ms breaches the 50ms gate"
    assert out["total"] == 1
