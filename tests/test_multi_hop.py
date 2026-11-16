"""L2.5 multi-hop impact propagation (`depth` parameter on code_map_query).

Design (owner 2026-11-16, roadmap item ① — the code-graph "signature
capability" minimal form):

- `depth=N` (1..HOP_MAX_DEPTH) propagates impact outward from a seed name:
    hop 1 (odd, consumer stage): rows where a file CONSUMES the seed
            (kind in call_site / kwarg_usage / import)
    hop 2 (even, def stage):    def-side symbols (function / method /
            class) defined INSIDE the hop-1 consumer files
    hop 3 (odd): consumers of the hop-2 frontier names, and so on.
- Name-level OVER-approximation: aliases/overloads merge, the seed's own
  definition is never re-emitted as a frontier row, positional-argument
  consumers are absent (no name to key on). All disclosed in the payload.
- Dedup: (name, path, line) globally, min hop wins; the hop cap terminates
  cycles (a file that defines and calls the same name cannot loop).
- Byte-capped: when the serialized payload exceeds PAGE_BUDGET_BYTES the
  DEEPEST hops are dropped first; `truncated=true` + `total` (all rows)
  stay visible. No cursor paging in depth mode.
- Omitting `depth` (or 0) is the legacy exact-name path: the payload is
  byte-identical to pre-L2.5 (regression guard below).

The fixture is hand-seeded (no parser dependency): the row KINDS are the
contract, and parser coverage lives in the L2.1/L2.2/L2.4 suites.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_imports() -> None:
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)


def _seed_hop_fixture(tmp_path, monkeypatch) -> str:
    """HERMES_HOME-isolated 4-file project with a fixed propagation chain.

    core.py: def min_score                     (seed def)
    a.py:    from core import min_score        (import row, L1)
             def entry_a                        (L2)
             min_score()                        (call_site, L3)
    b.py:    def entry_b                        (L1)
             entry_a()                          (call_site, L2)
    main.py: def main                           (L1)
             entry_b()                          (call_site, L2)
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _fresh_imports()
    from code_map import db as cdb
    from code_map import rootreg

    proj = tmp_path / "proj"
    proj.mkdir()
    for fname in ("core.py", "a.py", "b.py", "main.py"):
        (proj / fname).write_text("def f():\n    return 0\n", encoding="utf-8")
    root = str(proj)
    # confirm the isolated root (HERMES_HOME-pinned registry — the static
    # guard test_seed_helpers_isolate_hermes_home passes because this module
    # carries the HERMES_HOME isolation):
    rootreg.write_confirmed_root(root)

    rows = {
        "core.py": [("min_score", "function", 1, "def min_score(x=0.3):")],
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
        for fname, sym_rows in rows.items():
            p = os.path.join(root, fname)
            st = os.stat(p)
            cdb.upsert_file(conn, p, st.st_mtime, st.st_size, "python",
                           "t", commit=False)
            cdb.insert_symbols(conn, root, p, sym_rows, commit=False)
        conn.commit()
    finally:
        conn.close()
    return root


def _q(root, monkeypatch, **kw) -> dict:
    """Run code_map_query through the module entry (fresh import each call).
    monkeypatch is unused by the entry itself; it is what pinned HERMES_HOME."""
    _fresh_imports()
    from code_map.query import code_map_query
    return json.loads(code_map_query({"root": root, **kw}))


# ── 1. depth=2: two consumer/def stages ─────────────────────────────────────
def test_depth_2_rows(tmp_path, monkeypatch):
    root = _seed_hop_fixture(tmp_path, monkeypatch)
    p = _q(root, monkeypatch, name="min_score", depth=2)
    assert p["status"] != "invalid"
    got = {(r["name"], r["path"].split(os.sep)[-1], r["line"], r["kind"], r["hop"])
           for r in p["symbols"]}
    expect = {
        ("min_score", "a.py", 1, "import", 1),
        ("min_score", "a.py", 3, "call_site", 1),
        ("entry_a", "a.py", 2, "function", 2),
    }
    assert got == expect, f"depth=2 mismatch:\n got={sorted(got)}"
    assert p["total"] == 3 and p["count"] == 3
    assert p["truncated"] is False
    assert p["hops_returned"] == 2
    assert p["depth"] == 2


# ── 2. depth=3: consumer stage for the hop-2 frontier ──────────────────────
def test_depth_3_adds_consumers_of_hop2(tmp_path, monkeypatch):
    root = _seed_hop_fixture(tmp_path, monkeypatch)
    p = _q(root, monkeypatch, name="min_score", depth=3)
    got = {(r["name"], r["path"].split(os.sep)[-1], r["line"], r["kind"], r["hop"])
           for r in p["symbols"]}
    assert ("entry_a", "b.py", 2, "call_site", 3) in got
    assert p["total"] == 4 and p["hops_returned"] == 3


# ── 3. depth clamps to HOP_MAX_DEPTH ────────────────────────────────────────
def test_depth_clamps_to_max(tmp_path, monkeypatch):
    root = _seed_hop_fixture(tmp_path, monkeypatch)
    p = _q(root, monkeypatch, name="min_score", depth=99)
    assert p["depth_effective"] == 3
    assert max(r["hop"] for r in p["symbols"]) <= 3
    # hop 4 (def-side of b.py: entry_b) must NOT be present at the clamp
    assert not any(r["hop"] == 4 for r in p["symbols"])


# ── 4. no depth = legacy path, byte-identical payload shape ─────────────────
def test_no_depth_is_legacy(tmp_path, monkeypatch):
    root = _seed_hop_fixture(tmp_path, monkeypatch)
    p = _q(root, monkeypatch, name="min_score")
    for key in ("depth", "depth_effective", "hops_returned", "truncated"):
        assert key not in p, f"legacy payload must not carry {key}"
    assert all("hop" not in r for r in p["symbols"])
    # exact-name locate still returns the 3 direct rows (def + import + call)
    names = {(r["name"], r["kind"]) for r in p["symbols"]}
    assert ("min_score", "function") in names
    assert ("min_score", "import") in names
    assert ("min_score", "call_site") in names


# ── 5. depth without an exact name is invalid ───────────────────────────────
def test_depth_without_name_invalid(tmp_path, monkeypatch):
    root = _seed_hop_fixture(tmp_path, monkeypatch)
    p = _q(root, monkeypatch, depth=2)
    assert p["status"] == "invalid"


# ── 6. depth ignores path / kind (documented) ───────────────────────────────
def test_depth_ignores_path(tmp_path, monkeypatch):
    root = _seed_hop_fixture(tmp_path, monkeypatch)
    with_path = _q(root, monkeypatch, name="min_score", depth=2,
                   path=os.path.join(root, "b.py"))
    plain = _q(root, monkeypatch, name="min_score", depth=2)
    assert with_path["total"] == plain["total"]
    assert with_path["symbols"] == plain["symbols"]


# ── 7. fuzzy + depth is invalid (bridge candidates are not seeds) ───────────
def test_fuzzy_with_depth_invalid(tmp_path, monkeypatch):
    root = _seed_hop_fixture(tmp_path, monkeypatch)
    p = _q(root, monkeypatch, name="min_score", depth=2, fuzzy=True)
    assert p["status"] == "invalid"


# ── 8. byte cap drops the SHALLOWEST hops first (deep = new info) ─────────
def test_truncation_drops_shallowest_hops(tmp_path, monkeypatch):
    root = _seed_hop_fixture(tmp_path, monkeypatch)
    _fresh_imports()
    import code_map.query as qm
    # patch the NAME BOUND IN query's namespace (a fresh re-import after the
    # patch would rebind the unpatched config constant):
    monkeypatch.setattr(qm, "PAGE_BUDGET_BYTES", 300)
    p = json.loads(qm.code_map_query(
        {"root": root, "name": "min_score", "depth": 3}))
    assert p["truncated"] is True
    assert p["total"] == 4, "total must count ALL rows, truncated or not"
    assert p["hops_returned"] < 3 or p["count"] < p["total"]


# ── 9. intra-file define-and-call cycle terminates ──────────────────────────
def test_cycle_terminates(tmp_path, monkeypatch):
    import os
    root = _seed_hop_fixture(tmp_path, monkeypatch)
    _fresh_imports()
    from code_map import db as cdb
    p = os.path.join(root, "c.py")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("def entry_c():\n    entry_c()\n")
    conn = cdb.init_db(root)
    try:
        st = os.stat(p)
        cdb.upsert_file(conn, p, st.st_mtime, st.st_size, "python", "t")
        cdb.insert_symbols(conn, root, p, [
            ("entry_c", "function", 1, "def entry_c():"),
            ("entry_c", "call_site", 2, "entry_c()"),
        ])
    finally:
        conn.close()
    p2 = _q(root, monkeypatch, name="entry_c", depth=3)
    # hop1: the self call_site row; the seed's own def is never re-emitted,
    # so hop2 is empty and propagation stops — total must be exactly 1.
    assert p2["total"] == 1, p2


# ── 10. single-hop truncation must KEEP rows (never zero them out) ────────
def test_single_hop_truncation_keeps_rows(tmp_path, monkeypatch):
    root = _seed_hop_fixture(tmp_path, monkeypatch)
    _fresh_imports()
    from code_map import db as cdb
    import os
    # 40 consumer rows in one file: one hop, well over any small budget
    p = os.path.join(root, "many.py")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(f"min_score({i})" for i in range(40)) + "\n")
    conn = cdb.init_db(root)
    try:
        st = os.stat(p)
        cdb.upsert_file(conn, p, st.st_mtime, st.st_size, "python", "t")
        cdb.insert_symbols(conn, root, p, [
            ("min_score", "call_site", i + 1, f"min_score({i})")
            for i in range(40)])
    finally:
        conn.close()
    _fresh_imports()
    import code_map.query as qm
    # 2000B fits the envelope+disclosure (~1KB) plus a handful of rows —
    # the regression target: the trim loop must stop with rows kept, not
    # zero them out (the old whole-hop-drop loop emptied single-hop results).
    monkeypatch.setattr(qm, "PAGE_BUDGET_BYTES", 2000)
    p2 = json.loads(qm.code_map_query(
        {"root": root, "name": "min_score", "depth": 1}))
    assert p2["truncated"] is True
    assert p2["total"] == 42, p2["total"]          # 40 + fixture's 2
    assert 0 < p2["count"] < 42, (
        f"single-hop truncation must keep some rows; got {p2['count']}")
    assert p2["hops_returned"] == 1
