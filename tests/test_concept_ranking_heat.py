"""L2.4c-ranking: the def-side heat vs test-name rarity inversion.

L2.7 (locked-snapshot T-B retest) quantified the defect: the L2.4b
rank-order `hits DESC, tier, defmark, occ ASC` makes RARE names win —
a unique `test_*` def (occ=1) outranks a hot production def (occ=28)
on the def-side, because the original L2.4 accuracy guard ("rare names
first") inverts the signal once the candidate is a declaration: a
production symbol used across many files is exactly the answer a
concept bridge should surface. L2.4c splits the 4th order term on the
side: DEF-side candidates rank by occ DESC (cross-file heat),
USAGE-side-only candidates keep occ ASC (rarity — pure usage families
still prefer the distinctive one). The degraded gate-④ path
(no aggregates) is untouched.

Probe-isolation discipline (skill §1): EVERY test seeds AND queries
inside its own HERMES_HOME + fresh-imports scope — a query that runs
outside the fixture scope would resolve against the production
registry (the 2026-11-17 orphan-db lesson, test form).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_imports() -> None:
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)


def _seed_root(monkeypatch, tmp_path: Path) -> str:
    """HERMES_HOME-isolated root:
    widget_service  — def-side, occ=4 (def + 3 call rows), tier-2 for 'widget'
    _widget_probe   — def-side, occ=1 (unique test-ish def), tier-2
    rare_helper     — usage-only, occ=1, tier-2 for 'helper' (*_helper)
    hot_helper      — usage-only, occ=5, tier-2
    Files are stamped with the current parser generation (L2.3c): an
    unstamped seed reads as parser_gen_stale and would pollute any
    freshness-adjacent behavior under test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _fresh_imports()
    from code_map import db as cdb
    from code_map import rootreg, config

    proj = tmp_path / "rankproj"
    proj.mkdir()  # write_text does not create parent dirs (the FileNotFoundError class)
    for rel in ("a.py", "b.py", "c.py", "t.py"):
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
            ("hot_helper", "call_site", 3, "hot_helper()"),
        ],
        "c.py": [
            ("widget_service", "call_site", 1, "widget_service()"),
            ("hot_helper", "call_site", 2, "hot_helper()"),
            ("hot_helper", "call_site", 3, "hot_helper()"),
        ],
        "t.py": [
            ("_widget_probe", "function", 1, "def _widget_probe():"),
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
        conn.commit()
    finally:
        conn.close()
    return root


def _fuzzy(root, word):
    """Query WITHIN the isolated scope (fresh imports + HERMES_HOME set):
    a module-level helper called after the fixture's imports exist keeps
    the production registry out of the path."""
    import code_map.query as qm
    return json.loads(qm.code_map_query(dict(root=root, name=word, fuzzy=True)))


def test_def_side_heat_beats_unique_name(tmp_path, monkeypatch):
    """THE L2.7 inversion: both candidates are tier-2 def-side names for
    'widget'; the hot one (occ=4) must outrank the unique one (occ=1) —
    pre-L2.4c the occ ASC rarity term put _widget_probe first."""
    root = _seed_root(monkeypatch, tmp_path)
    out = _fuzzy(root, "widget")
    names = [r["name"] for r in out["symbols"]]
    assert names[0] == "widget_service", f"heat must lead the def-side: {names}"
    assert names[1] == "_widget_probe"
    # the def-side heat term must be disclosed (recall-surface sync)
    assert "heat" in out.get("disclosure", ""), out.get("disclosure")


def test_usage_side_keeps_rarity(tmp_path, monkeypatch):
    """Usage-only candidates (no def-side row for the name) keep the
    original L2.4 rarity direction: the RARE one (occ=1) outranks the
    hot one (occ=5) — the L2.4c split must not flip the usage side."""
    root = _seed_root(monkeypatch, tmp_path)
    out = _fuzzy(root, "helper")
    names = [r["name"] for r in out["symbols"]]
    assert names[0] == "rare_helper", f"rarity must lead the usage-side: {names}"
    assert names[1] == "hot_helper"


def test_degraded_path_unchanged(tmp_path, monkeypatch):
    """Gate-④ degraded ranking (no aggregates) stays (tier, ln)-ordered —
    the heat term must not leak into the degraded SQL (binding/order
    shift would be a ProgrammingError or a silent re-order)."""
    root = _seed_root(monkeypatch, tmp_path)
    _fresh_imports()
    from code_map import db as cdb
    sql, params = cdb._concept_rank_sql(["widget"], "root = ?", [root],
                                        200, 0, rarity=False)
    order_part = sql.split("ORDER BY", 1)[-1].split("LIMIT", 1)[0]
    assert "-occ" not in sql, "the degraded path must not carry the heat term"
    assert "ORDER BY tier, ln" in sql
