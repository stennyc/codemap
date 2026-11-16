"""L2.4b candidate-layer fixes (owner 2026-11-17, L2.6 campaign T-B findings):

The L2.4 concept bridge returned candidate ROWS (path,line,kind per row) —
a multi-row call_site/kwarg family drowned the one distinct NAME that
mattered (the T-B arm read `score` x6 rows and never saw `min_score`).
Plus three ranking defects, all db-verified on the filemem target:

1. NAME-DEDUP: one candidate row per distinct name (+ its occurrence
   count), so `limit=3` shows 3 NAMES, not 3 rows of one family.
2. UNDERSCORE STRIP: a leading underscore does not demote a prefix match
   (`_bringback_*` is tier-2 for token 'bringback', not tier-3).
3. DELIMITED SUFFIX: an underscore-delimited suffix is also tier-2
   (`confirm_supersede` for token 'supersede') — implementation-side
   names stop losing to short kwarg prefixes.
4. MULTI-TOKEN BOOST + DEF-SIDE PREFERENCE: with 2+ concept tokens, a
   name that STRONGLY matches every token (exact/prefix/delimited-suffix)
   outranks names that match only one; within the same rank, declaration
   rows (function/method/class/parameter/decorator) outrank usage rows
   (call_site/kwarg_usage/import) — the implementation, not its call
   sites, is the candidate.

The L2.4 contract in tests/test_concept_bridge.py is amended in the same
batch (dedup changes name-set/total semantics; underscore-strip changes
_heal_dirty's tier from 3 to 2).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from code_map.db import init_db, insert_symbols, upsert_file  # noqa: E402
from code_map.query import code_map_query  # noqa: E402
import time  # noqa: E402


def _seed(root, rows_by_path):
    from code_map import rootreg
    conn = init_db(root)
    try:
        for rel, rows in rows_by_path.items():
            fpath = os.path.join(root, rel)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("\n".join(r[3] for r in rows) + "\n")
            upsert_file(conn, fpath, time.time(), 0, "python", "l24b-test")
            insert_symbols(conn, root, fpath, rows)
    finally:
        conn.close()
    rootreg.write_confirmed_root(root)


def _q(root, name, **kw):
    args = {"root": root, "name": name, "fuzzy": True}
    args.update(kw)
    return json.loads(code_map_query(args))


# 'score' is a 6-row family (the T-B drown shape); 'min_score' / 'max_score'
# are single rows; 'score' is also the exact name.
ROWS = {
    "impl.py": [
        ("score", "method", 1, "def score(self, q)"),
        ("score", "call_site", 2, "score(...)"),
        ("score", "call_site", 3, "score(...)"),
        ("score", "call_site", 4, "score(...)"),
        ("score", "call_site", 5, "score(...)"),
        ("score", "call_site", 6, "score(...)"),
        ("min_score", "parameter", 20, "min_score: float = 0.3"),
        ("max_score", "parameter", 21, "max_score: float = 0.9"),
    ],
}


# ── defect 1: name-dedup ──────────────────────────────────────────────────
def test_fuzzy_candidates_are_distinct_names(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "score")
    names = [s["name"] for s in p["symbols"]]
    assert len(names) == len(set(names)), f"duplicated name rows: {names}"
    # the 6-row 'score' family is ONE candidate carrying its count
    by = {s["name"]: s for s in p["symbols"]}
    assert by["score"]["occ"] == 6, by["score"]
    # total counts DISTINCT candidate names, not rows
    assert p["total"] == 3, p["total"]


def test_limit_pages_names_not_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "score", limit=2)
    assert p["count"] == 2 and p["total"] == 3, (p["count"], p["total"])
    names = [s["name"] for s in p["symbols"]]
    assert "score" in names, names  # the 6-row family cannot crowd out page 1
    # cursor walk: names paginate, seen total == distinct total.
    # seen starts from page 0's names; the empty page (count=0) terminates
    # the walk WITHOUT being appended.
    seen = [s["name"] for s in p["symbols"]]
    cursor, pages = p["cursor"], 1
    while True:
        pg = _q(root, "score", limit=2, cursor=cursor)
        if pg["count"] == 0:
            break
        seen += [s["name"] for s in pg["symbols"]]
        cursor, pages = pg["cursor"], pages + 1
        assert pages < 10
    assert sorted(seen) == ["max_score", "min_score", "score"], seen


# ── defect 3: multi-token boost ──────────────────────────────────────────
def test_multi_token_all_strong_hits_outrank_single(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "min score")
    by = {s["name"]: s for s in p["symbols"]}
    # min_score STRONGLY matches both tokens (min prefix + _score suffix):
    # it carries the 2-token boost the old row-level ranking lost.
    assert by["min_score"]["hits"] == 2, by["min_score"]
    assert by["score"]["hits"] == 1, by["score"]
    names = [s["name"] for s in p["symbols"]]
    assert names[0] == "min_score", names  # the concept's own parameter first


# ── defect 2: underscore strip + delimited suffix ─────────────────────────
ROWS2 = {
    "b.py": [
        ("_bringback_footer", "function", 1, "def _bringback_footer()"),
        ("bringback_pin", "function", 2, "def bringback_pin()"),
        ("_supersede_confirm_candidates", "function", 10, "def _supersede_confirm_candidates()"),
        ("confirm_supersede", "function", 20, "def confirm_supersede()"),
        ("_tool_supersede", "function", 30, "def _tool_supersede()"),
        ("supersede_with", "kwarg_usage", 40, "f(supersede_with=1)"),
        ("supersede_max_candidates", "kwarg_usage", 41, "f(supersede_max_candidates=2)"),
        ("supersede_min_overlap", "kwarg_usage", 42, "f(supersede_min_overlap=3)"),
        ("bringback_session", "call_site", 50, "bringback_session(...)"),
        ("bringback_session", "call_site", 51, "bringback_session(...)"),
        ("bringback_session", "call_site", 52, "bringback_session(...)"),
    ],
}


def test_leading_underscore_keeps_prefix_tier2(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS2)
    p = _q(root, "bringback")
    by = {s["name"]: s for s in p["symbols"]}
    assert by["_bringback_footer"]["tier"] == 2, by["_bringback_footer"]
    assert by["bringback_pin"]["tier"] == 2, by["bringback_pin"]
    assert by["bringback_session"]["tier"] == 2, by["bringback_session"]


def test_delimited_suffix_promotes_implementation_names(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS2)
    p = _q(root, "supersede")
    by = {s["name"]: s for s in p["symbols"]}
    # suffix tier-2: all three implementation-side names reach tier 2
    assert by["confirm_supersede"]["tier"] == 2, by["confirm_supersede"]
    assert by["_tool_supersede"]["tier"] == 2, by["_tool_supersede"]
    assert by["_supersede_confirm_candidates"]["tier"] == 2, by["_supersede_confirm_candidates"]
    # DEF-SIDE PREFERENCE: the three function rows outrank the kwarg_usage
    # usage rows, which share the tier
    order = [s["name"] for s in p["symbols"]]
    impl = [n for n in order if n in ("confirm_supersede", "_tool_supersede",
                                       "_supersede_confirm_candidates")]
    kwarg = [n for n in order if n.startswith("supersede_")]
    assert impl and kwarg, (impl, kwarg)
    assert order.index(impl[-1]) < order.index(kwarg[0]), order


# ── legacy guard: single-token behavior on a name family unchanged ───────
def test_exact_single_token_still_top_ranked(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "min_score")
    assert p["symbols"][0]["name"] == "min_score"
    assert p["symbols"][0]["tier"] == 1
    assert p["symbols"][0]["occ"] == 1
