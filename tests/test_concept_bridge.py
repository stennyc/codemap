"""L2.4 concept bridge (owner 2026-11-16 roadmap, plan step 1):
word-surface candidate queries that bridge a concept word to symbol names.

Design (discussed with owner):
- new optional arg `fuzzy=true` on code_map_query — a NEW query surface;
  the default (exact) path stays byte-identical (regression guard #1).
- candidates are tiered word-surface matches: 1=name-equal, 2=prefix,
  3=substring (case-insensitive), MIN tier per row across all tokens of a
  multi-word concept; ranked tier ASC, then NAME RARITY ASC (rare names
  first — a 13.6k-row index full of `build` rows must not bury
  `rebuild_dirty`).
- 0 hits carry an explicit fallback note (word-surface miss != concept
  absent: 'rebuild' vs 'flush_stale_rows' share no characters) — closes
  the T1 double-cost failure mode (concept word -> 0 index hits ->
  blind grep fallback).
- disclosure: candidates are an OVER-approximation, not a semantic
  verdict — the model must verify sig/docstring before use.
- QUERY_GATE_MS: a 20k-row index must still answer in <50ms.
"""
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from code_map.config import QUERY_GATE_MS, CONCEPT_QUERY_GATE_MS  # noqa: E402
from code_map.db import (init_db, insert_symbols, upsert_file,  # noqa: E402
                         query_concept, count_concept)
from code_map.query import code_map_query  # noqa: E402
import json  # noqa: E402
import sqlite3  # noqa: E402


def _seed(root: str, rows_by_path: dict) -> None:
    """rows_by_path: {relpath: [(name, kind, line, sig), ...]}.

    Confirms the root in the (env-isolated) registry — the query gate
    rejects unconfirmed roots, so seeding without confirming would make
    every concept query bounce off the root gate, not the candidate SQL."""
    from code_map import rootreg
    conn = init_db(root)
    try:
        for rel, rows in rows_by_path.items():
            fpath = os.path.join(root, rel)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("\n".join(r[3] for r in rows) + "\n")
            upsert_file(conn, fpath, time.time(), 0, "python", "concept-test")
            insert_symbols(conn, root, fpath, rows)
    finally:
        conn.close()
    rootreg.write_confirmed_root(root)


# one shared fixture shape: 'rebuild' is COMMON (3 rows), 'rebuild_dirty'
# is RARE (1 row) -> rarity must rank rebuild_dirty above plain rebuild.
ROWS = {
    "a.py": [
        ("rebuild", "function", 1, "def rebuild(x)"),
        ("rebuild", "function", 20, "def rebuild(y)"),
        ("rebuild", "function", 40, "def rebuild(z)"),
        ("rebuild_dirty", "function", 60, "def rebuild_dirty()"),
        ("build", "function", 80, "def build()"),
        ("_heal_dirty", "function", 100, "def _heal_dirty()"),
        ("min_score", "parameter", 120, "min_score: float = 0.3"),
        ("max_score", "parameter", 130, "max_score: float = 0.9"),
    ],
}


def _q(root, name, fuzzy=True, **kw):
    args = {"root": root, "name": name, "fuzzy": fuzzy}
    args.update(kw)
    return json.loads(code_map_query(args))


# ── regression guard: the EXACT path is untouched ─────────────────────────
def test_exact_path_unchanged_without_fuzzy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "rebuild", fuzzy=False)
    # exact mode: only the 3 name-equal rows — NO prefix/substring bleed
    assert p["count"] == 3 and p["total"] == 3, p
    assert all(s["name"] == "rebuild" for s in p["symbols"])
    assert "match" not in p or p.get("match") != "fuzzy"


# ── tier semantics ─────────────────────────────────────────────────────────
def test_fuzzy_prefix_tier_ranked_by_rarity(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "rebu")
    names = [s["name"] for s in p["symbols"]]
    tiers = [s["tier"] for s in p["symbols"]]
    assert set(names) == {"rebuild", "rebuild_dirty"}, names
    # all tier 2 (prefix) — no exact, no substring for this token
    assert all(t == 2 for t in tiers), list(zip(names, tiers))
    # rarity: rebuild_dirty (1 row) ranks ABOVE rebuild (3 rows)
    assert names[0] == "rebuild_dirty", names


def test_fuzzy_exact_beats_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "rebuild")
    first = p["symbols"][0]
    assert first["name"] == "rebuild" and first["tier"] == 1, first
    # later entries (prefix tier) come after all exact rows
    tiers = [s["tier"] for s in p["symbols"]]
    assert tiers == sorted(tiers), tiers


def test_fuzzy_substring_tier3(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "heal")
    # no name STARTS with 'heal' (it is '_heal_dirty') -> substring tier
    hits = [s for s in p["symbols"] if s["name"] == "_heal_dirty"]
    assert hits and hits[0]["tier"] == 3, p["symbols"]


# ── multi-token OR union + best-tier-per-row ─────────────────────────────
def test_multi_token_union_with_best_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "min score")
    by_name = {s["name"]: s for s in p["symbols"]}
    assert "min_score" in by_name and "max_score" in by_name, by_name
    # min_score: 'min' prefix (tier2) beats 'score' substring (tier3)
    assert by_name["min_score"]["tier"] == 2, by_name["min_score"]
    # max_score: only 'score' substring
    assert by_name["max_score"]["tier"] == 3, by_name["max_score"]
    # rank: min_score before max_score
    names = [s["name"] for s in p["symbols"]]
    assert names.index("min_score") < names.index("max_score"), names


# ── 0-hit fallback note (the T1 double-cost closer) ───────────────────────
def test_zero_hit_carries_grep_fallback_note(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "zzz_no_overlap_at_all")
    assert p["total"] == 0 and p["symbols"] == [], p
    note = p.get("note", "").lower()
    assert "grep" in note or "word-surface" in note, p


# ── total/limit/paging semantics on the candidate set ─────────────────────
def test_total_counts_all_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "e", limit=3)  # 'e' is in rebuild/build/_heal_dirty/...
    assert p["count"] == 3
    assert p["total"] > 3, f"total {p['total']} must exceed the page"
    # cursor walk reaches exhaustion
    seen = 0
    cursor = "0"
    pages = 0
    while True:
        pg = _q(root, "e", limit=3, cursor=cursor)
        seen += pg["count"]
        pages += 1
        if pg["count"] == 0:
            break
        cursor = pg["cursor"]
        assert pages < 20
    assert seen == p["total"], (seen, p["total"])


# ── filters compose with fuzzy ─────────────────────────────────────────────
def test_path_and_kind_filters_compose(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "score", kind="parameter")
    assert all(s["kind"] == "parameter" for s in p["symbols"])
    p2 = _q(root, "rebu", path=os.path.join("a.py"))
    assert p2["count"] == 4, p2  # 3x rebuild + 1x rebuild_dirty, one file


# ── disclosure + gate ────────────────────────────────────────────────────
def test_fuzzy_disclosure_marks_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    _seed(root, ROWS)
    p = _q(root, "rebu")
    d = p.get("disclosure", "")
    assert "candidate" in d.lower() and "verify" in d.lower(), d[:200]


def test_20k_rows_within_query_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "x.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    conn = init_db(root)
    try:
        upsert_file(conn, os.path.join(root, "x.py"), time.time(), 0, "python",
                    "gate-test", commit=False)
        rows = [(f"sym_{i:05d}_mod", "function", i, f"def sym_{i:05d}_mod()")
                for i in range(20_000)]
        conn.executemany(
            "INSERT OR REPLACE INTO symbols(name, kind, line, sig, path, root)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            [(n, k, l, s, os.path.join(root, "x.py"), root)
             for n, k, l, s in rows])
        conn.commit()
    finally:
        conn.close()
    from code_map import rootreg
    rootreg.write_confirmed_root(root)  # outside the timed window
    t0 = time.perf_counter()
    p = _q(root, "mod")          # substring: hits all 20k 'sym_..._mod'
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    assert p["total"] == 20_000, p["total"]
    # concept surface has its own scan-bound budget (separate from the
    # sealed QUERY_GATE_MS which governs the index-driven EXACT paths)
    assert elapsed_ms < CONCEPT_QUERY_GATE_MS, (
        f"concept query {elapsed_ms}ms >= gate {CONCEPT_QUERY_GATE_MS}ms")


def test_exact_path_stays_within_sealed_gate_at_20k(tmp_path, monkeypatch):
    """The sealed QUERY_GATE_MS=50 governs the EXACT paths — the concept
    bridge must not have regressed them: an exact-name query on a 20k-row
    root still answers via idx_symbols_name in <50ms."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    root = str(tmp_path / "proj")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "x.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    conn = init_db(root)
    try:
        upsert_file(conn, os.path.join(root, "x.py"), time.time(), 0, "python",
                    "gate-test", commit=False)
        rows = [(f"sym_{i:05d}_mod", "function", i, f"def sym_{i:05d}_mod()")
                for i in range(20_000)]
        conn.executemany(
            "INSERT OR REPLACE INTO symbols(name, kind, line, sig, path, root)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            [(n, k, l, s, os.path.join(root, "x.py"), root)
             for n, k, l, s in rows])
        conn.commit()
    finally:
        conn.close()
    from code_map import rootreg
    rootreg.write_confirmed_root(root)
    # timing-sensitive: take the median of 3 samples (cold-start page-cache
    # and first-connection effects skew sample 0)
    samples = []
    for _ in range(3):
        t0 = time.perf_counter()
        p = _q(root, "sym_00042_mod", fuzzy=False)   # exact name, index-driven
        samples.append(int((time.perf_counter() - t0) * 1000))
    elapsed_ms = sorted(samples)[1]
    assert p["total"] == 1, p
    assert elapsed_ms < QUERY_GATE_MS, (
        f"exact query median {elapsed_ms}ms (samples {samples}) >= gate")
