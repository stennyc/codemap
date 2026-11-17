"""L2.8j: the name_vocab prune is index-driven + amortized (one per build),
and the HOST refresh face clears parser-generation drift (L2.8i).

Root cause this fixes (measured 2026-11-18 on the live 32.5k-row db copy):
the L2.4d-F1 per-file vocab prune (correlated NOT EXISTS) planned as
O(vocab x root_symbols) — SCAN name_vocab (3,011 rows) + a correlated
scalar subquery that only seeks idx_symbols_root_lname on root= (the
lname correlation is NOT index-driven) => ~15 s PER FILE. The L2.3c
generation-mismatch re-parse path calls remove_paths per stale file, so a
62-file drift took ~16 min of wall time (the observed 23-min re-stamp,
amplified by the kernel/gateway-heal contention).

Fixes pinned here:
- prune_orphan_vocab: single-pass set-op (lname NOT IN the DISTINCT
  lower(name) projection over symbols — idx_symbols_lname-driven, ms at
  live scale; the plan must NOT be correlated);
- remove_paths gains prune_vocab/commit flags (default behavior keeps the
  single-file-deletion semantics: prune after the deletes);
- BuildScanner amortizes: the gen-mismatch loop passes prune_vocab=False
  and prunes ONCE after the loop;
- state._run_build (the L3/host refresh face) now re-parses
  generation-stale on-disk files (diff via db.gen_stale_paths) and clears
  their phantom rows BEFORE re-parse (L2.8i — previously the refresh was a
  no-op on gen drift and the payload note was untrue).
"""
import os
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
for p in (str(PLUGIN_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _fresh_imports() -> None:
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)


def _seed_root(monkeypatch, tmp_path: Path, files=4) -> str:
    """Isolated confirmed root with `files` source files (x = 1 content)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _fresh_imports()
    from code_map import rootreg
    proj = tmp_path / "pruneproj"
    proj.mkdir()
    for i in range(files):
        (proj / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
    root = str(proj)
    rootreg.write_confirmed_root(root)
    return root


def _seed_symbols(root: str, n_files: int, names_per_file: int,
                  parser_gen: str = "") -> None:
    """Bulk-seed the symbols + files tables under a given generation stamp
    (mimics a stale-generation index: rows whose names the CURRENT parser
    would not emit -> phantom rows)."""
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        for i in range(n_files):
            p = (f"{root}\\phantom{i}.py" if os.name == "nt"
                 else f"{root}/phantom{i}.py")
            cdb.upsert_file(conn, p, 0.0, 1, "python", "seed",
                            parser_gen=parser_gen)
            rows = [(f"ghost_fn_{i}_{j}", "function", i * 100 + j,
                     f"def ghost_fn_{i}_{j}():")
                    for j in range(names_per_file)]
            cdb.insert_symbols(conn, root, p, rows)
        conn.commit()
    finally:
        conn.close()


# ── 1. semantics: orphans pruned, survivors kept (byte-exact sets) ───────

def test_prune_orphan_vocab_semantics(monkeypatch, tmp_path):
    root = _seed_root(monkeypatch, tmp_path, files=1)
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        p = f"{root}\\m0.py" if os.name == "nt" else f"{root}/m0.py"
        cdb.upsert_file(conn, p, 0.0, 1, "python", "s",
                        parser_gen="l2.8j-test")
        cdb.insert_symbols(conn, root, p,
                           [("kept_name", "function", 1, "def kept_name():"),
                            ("gone_name", "function", 2, "def gone_name():")])
        conn.commit()
        # now 'gone_name' loses its LAST symbol row -> its vocab row is an
        # orphan; 'kept_name' survives.
        cdb.remove_paths(conn, {p}, prune_vocab=False)
        cdb.insert_symbols(conn, root, p,
                           [("kept_name", "function", 1, "def kept_name():")])
        conn.commit()
        before = {r[0] for r in conn.execute("SELECT lname FROM name_vocab")}
        cdb.prune_orphan_vocab(conn)
        conn.commit()
        after = {r[0] for r in conn.execute("SELECT lname FROM name_vocab")}
        assert "kept_name" in after, "a name with a live symbol row must survive"
        assert "gone_name" not in after, "the orphan must be pruned"
        assert before > after, "the prune must actually delete something"
    finally:
        conn.close()


# ── 2. plan pin: the prune must NOT plan as a correlated subquery ───────

def test_prune_plan_not_correlated(monkeypatch, tmp_path):
    root = _seed_root(monkeypatch, tmp_path, files=1)
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        plan = " ".join(
            r[3] for r in conn.execute(
                "EXPLAIN QUERY PLAN "
                "DELETE FROM name_vocab WHERE lname NOT IN "
                "(SELECT DISTINCT lower(name) FROM symbols)"))
        assert "CORRELATED" not in plan, \
            "the prune must be a single-pass set op, not a correlated walk"
    finally:
        conn.close()


# ── 3. performance gate: live-scale fixture, prune stays in ms ───────────

def test_prune_index_driven_within_gate_at_live_scale(monkeypatch, tmp_path):
    """At the live filemem scale (3,011 vocab x 32,522 symbols) the OLD
    correlated prune measured ~15 s; the new single-pass set op must stay
    far under the 500 ms gate (idle)."""
    root = _seed_root(monkeypatch, tmp_path, files=1)
    _seed_symbols(root, n_files=10, names_per_file=3_252,
                  parser_gen="l2.3c-1")  # 32,520 rows / 3,252 distinct
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        conn.commit()  # settle the fixture
        t0 = time.time()
        cdb.prune_orphan_vocab(conn)
        conn.commit()
        elapsed_ms = (time.time() - t0) * 1000
        assert elapsed_ms < 500, \
            f"prune at live scale must be index-driven: {elapsed_ms:.0f} ms"
    finally:
        conn.close()


# ── 4. scanner amortization: one prune per BUILD, not per stale file ─────

def test_scanner_prune_runs_once_not_per_file(monkeypatch, tmp_path):
    root = _seed_root(monkeypatch, tmp_path, files=3)
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        # stamp all three files under an OLD generation (the L2.3c
        # mismatch shape) and give file 0 a phantom name to be pruned.
        for i in range(3):
            p = f"{root}\\m{i}.py" if os.name == "nt" else f"{root}/m{i}.py"
            cdb.upsert_file(conn, p, 0.0, 1, "python", "s",
                            parser_gen="l2.3c-1")
            cdb.insert_symbols(conn, root, p,
                               [(f"m{i}_fn", "function", 1, f"def m{i}_fn():")])
        cdb.insert_symbols(conn, root,
                           f"{root}\\m0.py" if os.name == "nt" else f"{root}/m0.py",
                           [("phantom_only_m0", "function", 9,
                            "def phantom_only_m0():")])
        conn.commit()
        conn.close()
    finally:
        conn.close()

    _fresh_imports()
    import code_map.scanner as sc
    calls = {"n": 0}
    real = cdb.prune_orphan_vocab

    def counting(conn_):
        calls["n"] += 1
        real(conn_)

    monkeypatch.setattr(sc, "prune_orphan_vocab", counting, raising=False)
    stats = sc.BuildScanner(root, "l2.8j-amort").run()
    assert stats["gen_reparsed"] == 3, "all three stale files re-parsed"
    assert calls["n"] == 1, \
        f"the prune must run ONCE per build, got {calls['n']} calls"
    conn = cdb.init_db(root)
    try:
        gen = {r[0] for r in conn.execute("SELECT DISTINCT parser_gen FROM files")}
        assert gen == {sc.config.PARSER_GENERATION}, "files re-stamped"
        assert conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE name='phantom_only_m0'"
        ).fetchone()[0] == 0, "the phantom row must be cleared on re-parse"
        assert conn.execute(
            "SELECT COUNT(*) FROM name_vocab WHERE lname='phantom_only_m0'"
        ).fetchone()[0] == 0, "the pruned orphan stays pruned"
    finally:
        conn.close()


# ── 5. L2.8i: the HOST refresh face clears parser-generation drift ───────

def test_host_refresh_clears_gen_drift(monkeypatch, tmp_path):
    root = _seed_root(monkeypatch, tmp_path, files=2)
    _fresh_imports()
    from code_map import db as cdb
    from code_map import state, config

    conn = cdb.init_db(root)
    try:
        for i in range(2):
            p = f"{root}\\m{i}.py" if os.name == "nt" else f"{root}/m{i}.py"
            cdb.upsert_file(conn, p, 0.0, 1, "python", "s",
                            parser_gen="l2.3c-1")
            cdb.insert_symbols(conn, root, p,
                               [(f"oldgen_fn_{i}", "function", 1,
                                f"def oldgen_fn_{i}():")])
        conn.commit()
        # pre-fix shape: the staleness face reports the drift
        from code_map import query
        assert query._staleness(root, conn) == "parser_gen_stale"
    finally:
        conn.close()

    # the host face (L3 refresh / confirm build): re-parse + re-stamp
    state._run_build(root)

    conn = cdb.init_db(root)
    try:
        from code_map import query
        assert query._staleness(root, conn) == "fresh", \
            "the refresh face must clear the generation drift (L2.8i)"
        gen = {r[0] for r in conn.execute("SELECT DISTINCT parser_gen FROM files")}
        assert gen == {config.PARSER_GENERATION}, \
            "every file row re-stamped to the current generation"
        n_old = conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE name LIKE 'oldgen_fn_%'"
        ).fetchone()[0]
        assert n_old == 0, "stale-generation phantom rows cleared on re-parse"
    finally:
        conn.close()


# ── 6. backward compat: default remove_paths still prunes ────────────────

def test_remove_paths_default_still_prunes(monkeypatch, tmp_path):
    root = _seed_root(monkeypatch, tmp_path, files=1)
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        p = f"{root}\\m0.py" if os.name == "nt" else f"{root}/m0.py"
        cdb.upsert_file(conn, p, 0.0, 1, "python", "s", parser_gen="g")
        cdb.insert_symbols(conn, root, p,
                           [("solo_name", "function", 1, "def solo_name():")])
        conn.commit()
        cdb.remove_paths(conn, {p})  # default: prune after deletes
        orphans = conn.execute(
            "SELECT COUNT(*) FROM name_vocab WHERE lname='solo_name'"
        ).fetchone()[0]
        assert orphans == 0, "the legacy per-call prune must still run by default"
    finally:
        conn.close()


# ── 7. the refresh-face note pin (L2.8i disclosure alignment) ────────────

def test_refresh_face_note_stays_accurate(monkeypatch, tmp_path):
    _seed_root(monkeypatch, tmp_path, files=1)
    _fresh_imports()
    from code_map import query
    payload = query._attach_gen_stale_note({"staleness": "parser_gen_stale"})
    note = payload.get("note", "")
    assert "code_map_refresh" in note
    assert "clears each stale file" in note and "re-stamps" in note, \
        "the note promises the refresh face clears + re-stamps — L2.8i " \
        "makes that true, so the pin is the contract"
