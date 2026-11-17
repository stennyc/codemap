"""L2.5b: the seed file's own public surface in multi-hop propagation.

L2.6 T-C finding: `depth=N` even hops surface def-side symbols in the
CONSUMER files only — the seed's own module-level entries (host hooks in
an `__init__.py`, register(), ...) never appear, so "impact entry
points" under-reports the layer that hosts actually hook.

Design (owner plan step 2):
- The seed's def files (files that carry def-side rows for the seed
  name: function/method/class) contribute their def-side public surface
  at the FIRST EVEN hop (hop 2): the seed's own def row is re-emitted as
  an entry candidate (it was deliberately excluded from consumer hops —
  that rule is unchanged), plus the sibling module-level defs in the
  same files.
- Seeds with NO def-side rows (e.g. a parameter name) get NO seed-face
  rows — the output is the pre-L2.5b shape (regression guard).
- depth=1 (no even hop) is unchanged.
- Global (name, path, line) dedup with min hop still holds; the byte
  cap still drops SHALLOW hops first, so seed-face rows (hop 2) survive
  over hop-1 rows when over budget.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_imports() -> None:
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)


def _seed(tmp_path, monkeypatch) -> str:
    """core.py carries the seed def + a public face; a.py/b.py/main.py
    form the propagation chain (same shape as test_multi_hop)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _fresh_imports()
    from code_map import db as cdb
    from code_map import rootreg

    proj = tmp_path / "sfproj"
    (proj / "sub").mkdir(parents=True)
    for rel in ("core.py", "a.py", "b.py", "main.py"):
        (proj / rel).write_text("def f():\n    return 0\n", encoding="utf-8")
    root = str(proj)
    rootreg.write_confirmed_root(root)

    p = Path(root)
    rows = {
        "core.py": [
            ("min_score", "function", 1, "def min_score(x=0.3):"),
            ("register", "function", 2, "def register():"),
            ("filemem_auto_retrieve", "function", 3,
             "def filemem_auto_retrieve():"),
        ],
        "a.py": [
            ("min_score", "import", 1, "from core import min_score"),
            ("entry_a", "function", 2, "def entry_a():"),
            ("min_score", "call_site", 3, "min_score()"),
        ],
        "b.py": [
            ("entry_b", "function", 1, "def entry_b():"),
            ("entry_a", "call_site", 2, "entry_a()"),
        ],
        "main.py": [
            ("main", "function", 1, "def main():"),
            ("entry_b", "call_site", 2, "entry_b()"),
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


def test_seed_face_surfaces_at_first_even_hop(tmp_path, monkeypatch):
    root = _seed(tmp_path, monkeypatch)
    out = _q(root, name="min_score", depth=2)
    assert out["seed_face"] is True
    hop2 = {r["name"] for r in out["symbols"] if r["hop"] == 2}
    assert "min_score" in hop2            # the seed's own def, re-emitted
    assert "register" in hop2             # sibling module-level entry
    assert "filemem_auto_retrieve" in hop2
    # consumer-stage rows still present and correctly hop-numbered:
    hop1 = {r["name"] for r in out["symbols"] if r["hop"] == 1}
    assert "min_score" in hop1            # a.py's import + call_site rows


def test_depth_1_unchanged(tmp_path, monkeypatch):
    root = _seed(tmp_path, monkeypatch)
    out = _q(root, name="min_score", depth=1)
    assert out["seed_face"] is not True
    assert {r["hop"] for r in out["symbols"]} == {1}
    assert "register" not in {r["name"] for r in out["symbols"]}


def test_seed_without_def_rows_gets_no_seed_face(tmp_path, monkeypatch):
    """A seed name with no def-side rows (call_site-only, e.g. an
    imported helper) must not invent a seed face — pre-L2.5b shape."""
    root = _seed(tmp_path, monkeypatch)
    out = _q(root, name="entry_a", depth=2)
    # entry_a HAS a def row in a.py (function) — use a def-less name:
    # 'main' is called by nobody and defines main()... use 'entry_b':
    # it too has a def. The def-less case: add one on the fly.
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        cdb.insert_symbols(
            conn, root, str(Path(root) / "z.py"),
            [("ghost", "call_site", 1, "ghost()")], commit=True)
    finally:
        conn.close()
    out = _q(root, name="ghost", depth=2)
    assert out["seed_face"] is not True
    # ghost has no def rows -> no seed face, and no def-side pool at
    # hop 2 either; the ONLY row is z.py's own call_site (hop 1):
    assert out["total"] == 1
    assert out["symbols"][0]["hop"] == 1 and out["symbols"][0]["name"] == "ghost"


def test_seed_face_dedup_and_min_hop(tmp_path, monkeypatch):
    root = _seed(tmp_path, monkeypatch)
    out = _q(root, name="min_score", depth=2)
    triples = [(r["name"], r["path"], r["line"]) for r in out["symbols"]]
    assert len(triples) == len(set(triples))


def test_legacy_depth_without_seed_face_flag_shape(tmp_path, monkeypatch):
    """The depth payload gains the seed_face flag only; every OTHER field
    of the legacy depth payload keeps its name and meaning."""
    root = _seed(tmp_path, monkeypatch)
    out = _q(root, name="min_score", depth=2)
    for field in ("symbols", "count", "total", "depth", "depth_effective",
                  "hops_returned", "truncated", "frontier_capped",
                  "staleness", "cursor", "disclosure"):
        assert field in out, f"legacy field missing: {field}"
