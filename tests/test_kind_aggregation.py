"""L2.3b root-level / directory-level kind aggregation on code_map_query.

The L2.6 T-A arm had to page 128 per-file queries to enumerate 4 extends
rows — `kind=` alone (no name, no path) was refused by the name/path gate.
This lands the aggregation branch: `kind` alone (optionally with a file or
directory path) returns the kind rows across the whole target (or subtree),
paged + total + the existing binary byte-cap trim.

Contract:
- kind-only call (no name): whole-target aggregation; `path` = directory
  -> subtree (LIKE prefix, os.sep per the b72fb30 discipline), file ->
  exact-file (existing semantics); payload carries kind_aggregation=true.
- name + kind calls are UNCHANGED (byte-identical legacy path).
- unknown kind on a kind-only call is rejected with the valid list.
- root-only (no name, no path, no kind) is still invalid.
- the O(N) kind scan stays inside the 50ms exact-path gate at 35k-row
  scale (steady-state min-of-N, per the gate-object discipline).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_imports() -> None:
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)


def _seed_kind_fixture(tmp_path, monkeypatch) -> str:
    """HERMES_HOME-isolated project:
    core.py:  class A (1) / class B(A) (2) / class C(B) (3) /
              def f(x=0.3) parameter x (4) / @t def g (5, decorator t)
    sub/x.py: class D (1) / class E(D) (2)
    main.py:  def main (1) / main() call_site (2)
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _fresh_imports()
    from code_map import db as cdb
    from code_map import rootreg

    proj = tmp_path / "kindproj"
    (proj / "sub").mkdir(parents=True)
    for rel in ("core.py", "sub/x.py", "main.py"):
        (proj / rel).write_text("class A:\n    pass\n", encoding="utf-8")
    root = str(proj)
    rootreg.write_confirmed_root(root)

    p = Path(root)
    rows = {
        "core.py": [
            ("A", "class", 1, "class A:"),
            ("B", "class", 2, "class B(A):"),
            ("A", "extends", 2, "class B(A):"),
            ("C", "class", 3, "class C(B):"),
            ("B", "extends", 3, "class C(B):"),
            ("f", "function", 4, "def f(x=0.3):"),
            ("x", "parameter", 4, "def f(x=0.3):"),
            ("g", "function", 5, "@t\ndef g():"),
            ("t", "decorator", 5, "@t"),
        ],
        "sub/x.py": [
            ("D", "class", 1, "class D:"),
            ("E", "class", 2, "class E(D):"),
            ("D", "extends", 2, "class E(D):"),
        ],
        "main.py": [
            ("main", "function", 1, "def main():"),
            ("main", "call_site", 2, "main()"),
        ],
    }
    conn = cdb.init_db(root)
    try:
        for rel, sym_rows in rows.items():
            cdb.insert_symbols(conn, root, str(p / rel), sym_rows, commit=True)
    finally:
        conn.close()
    return root


def _q(root, **kw):
    from code_map import query
    return json.loads(query.code_map_query(dict(root=root, **kw)))


def test_kind_only_root_aggregation(tmp_path, monkeypatch):
    root = _seed_kind_fixture(tmp_path, monkeypatch)
    out = _q(root, kind="extends")
    # the success payload carries no "status" key (that field marks the
    # invalid paths) — assert absence-of-invalid, not absence-of-key:
    assert out.get("status") != "invalid"
    assert out["kind_aggregation"] is True
    names = {r["name"] for r in out["symbols"]}
    assert names == {"A", "B", "D"}           # all 3 extends rows, all files
    assert out["total"] == 3
    # every row is path-qualified (per-file distribution is the point):
    assert all(r["path"] for r in out["symbols"])


def test_kind_only_directory_subtree(tmp_path, monkeypatch):
    root = _seed_kind_fixture(tmp_path, monkeypatch)
    out = _q(root, kind="extends", path="sub")
    assert out["kind_aggregation"] is True
    assert out["total"] == 1
    assert out["symbols"][0]["name"] == "D"


def test_kind_only_file_path_exact(tmp_path, monkeypatch):
    root = _seed_kind_fixture(tmp_path, monkeypatch)
    out = _q(root, kind="extends", path="core.py")
    assert out["total"] == 2
    assert {r["name"] for r in out["symbols"]} == {"A", "B"}


def test_kind_only_out_of_root_rejected(tmp_path, monkeypatch):
    root = _seed_kind_fixture(tmp_path, monkeypatch)
    out = _q(root, kind="extends", path="../elsewhere")
    assert out["status"] == "invalid"


def test_unknown_kind_rejected_with_valid_list(tmp_path, monkeypatch):
    root = _seed_kind_fixture(tmp_path, monkeypatch)
    out = _q(root, kind="bogus_kind")
    assert out["status"] == "invalid"
    assert "extends" in out["note"] and "call_site" in out["note"]


def test_name_plus_kind_is_legacy_path(tmp_path, monkeypatch):
    root = _seed_kind_fixture(tmp_path, monkeypatch)
    out = _q(root, name="A", kind="extends")
    assert out.get("kind_aggregation") is not True   # legacy shape, no flag
    assert out["total"] == 1 and out["symbols"][0]["name"] == "A"


def test_root_only_still_invalid(tmp_path, monkeypatch):
    root = _seed_kind_fixture(tmp_path, monkeypatch)
    out = _q(root)
    assert out["status"] == "invalid"


def test_kind_aggregation_pages_via_cursor(tmp_path, monkeypatch):
    root = _seed_kind_fixture(tmp_path, monkeypatch)
    out = _q(root, kind="class")                    # 5 class rows (A,B,C,D,E)
    assert out["total"] == 5
    assert len(out["symbols"]) == 5                 # small fixture, 1 page
    out2 = _q(root, kind="class", limit=2, cursor="0")
    assert len(out2["symbols"]) == 2 and out2["cursor"] == "2"
    out3 = _q(root, kind="class", limit=2, cursor="2")
    assert len(out3["symbols"]) == 2 and out3["total"] == 5
    out4 = _q(root, kind="class", limit=2, cursor="4")
    assert len(out4["symbols"]) == 1 and out4["total"] == 5


def test_kind_scan_gate_at_35k_rows(tmp_path, monkeypatch):
    """Steady-state gate-object check at the live filemem scale: a kind
    scan must stay inside the exact-path 50ms gate (min-of-N, warm-up
    uncounted — the gate-object discipline, not whole-call wall time)."""
    import time as _time
    root = _seed_kind_fixture(tmp_path, monkeypatch)
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        p = Path(root)
        big = str(p / "big.py")
        (p / "big.py").write_text("x = 1\n", encoding="utf-8")
        rows = [(f"n{i}", "function", i + 1, f"def n{i}():") for i in range(35000)]
        cdb.insert_symbols(conn, root, big, rows, commit=True)
    finally:
        conn.close()
    # warm-up (one uncounted call); binding `out` so the post-loop
    # assertions have a definite value even under static analysis
    out = _q(root, kind="function")
    best = float("inf")
    for _ in range(5):
        out = _q(root, kind="function")
        best = min(best, out["query_ms"])
    assert best <= 50, f"kind aggregation {best}ms breaches the 50ms gate"
    # 35000 big.py rows + the fixture seed's 3 function rows (f, g, main)
    assert out["total"] == 35003
