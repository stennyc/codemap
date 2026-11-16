"""Process-isolated code_map full build (owner 2026-11-17).

The fire-and-forget full-build paths (confirm_root / refresh_root / the
read-side pull) previously ran ``_run_build`` on a SAME-PROCESS daemon
thread — a heavy tree-sitter parse + sqlite commit that shares the main
process's GIL / CPU / memory, i.e. it '打断' (soft-contends with) the main
process's concurrent work. Owner ruling: run the build in a CHILD process
(code indexing is pure mechanical — zero LLM, so the in-process
``main_runtime`` / in-memory panel constraints that rule out subprocesses
for filemem simply do not exist here), so the main process is truly
isolated (no CPU / GIL / memory / sqlite-lock sharing).

Case map:
  1. ``test_entry_builds_in_child_process``: the entry script runs a real
     build for a tiny target in a separate process (exit 0) and the index
     db lands.
  2. ``test_entry_failure_is_visible``: a build failure exits non-zero +
     prints a traceback to stderr (mirrors _safe_run_build's fail-visible
     posture — never a silent no-op).
  3. ``test_start_build_process_spawns_tracks_reaps``:
     ``state._start_build_process`` spawns a Popen whose pid differs from
     the main process, tracks it in ``_live_build_processes``, and
     ``join_live_build_threads`` reaps it; the index lands.
  4. ``test_spawn_hiccup_falls_back_to_thread``: if Popen itself raises,
     the build falls back to the in-process daemon thread (the pre-change
     behaviour) so a build is NEVER lost to a spawn failure — fail-soft.

Run (same recipe as the sibling tests): a python that can ``import
pytest`` + the plugin parent on sys.path; the child build gets tree-sitter
via CODE_MAP_PY_SITE pointing at the plugin's .venv site-packages.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import sys as _sys
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent  # .../plugins (bootstrap)
if str(_PLUGIN_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_PLUGIN_ROOT))
import pytest  # noqa: E402

from code_map import state as _state  # noqa: E402
from code_map.config import root_db_path  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parent.parent
ENTRY = PLUGIN_DIR / "build_subprocess_entry.py"


def _ts_site() -> str | None:
    """First plugin .venv site-packages layout that actually has tree_sitter
    (so the child build is meaningful). None -> the build degrades to an
    empty index but still exits 0 + creates the db."""
    for layout in (".venv/Lib/site-packages",
                    ".venv/lib/python3.13/site-packages",
                    ".venv/lib/python3.14/site-packages",
                    ".venv/lib/python3.12/site-packages",
                    ".venv/lib/python3.11/site-packages"):
        cand = PLUGIN_DIR / layout
        if (cand / "tree_sitter").is_dir():
            return str(cand)
    return None


def _mk_target(name: str) -> Path:
    t = Path(f"/tmp/cm_subproc_{name}")
    t.mkdir(parents=True, exist_ok=True)
    (t / "a.py").write_text("def fa():\n    return 1\n\n\nclass Cb:\n    x = 2\n")
    (t / "b.py").write_text("def fb():\n    pass\n")
    return t


def _cleanup(*paths) -> None:
    import shutil
    _state.join_live_build_threads(timeout=10.0)
    for p in paths:
        try:
            shutil.rmtree(p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 1. entry script builds in a child process
# ---------------------------------------------------------------------------
def test_entry_builds_in_child_process(tmp_path, monkeypatch):
    target = _mk_target("entry")
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    site = _ts_site()
    if site:
        monkeypatch.setenv("CODE_MAP_PY_SITE", site)
    try:
        res = subprocess.run(
            [sys.executable, str(ENTRY), str(target)],
            capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, f"entry exit {res.returncode}: {res.stderr[-400:]}"
        # the index db lands at the path BOTH the test + the child compute
        # from the same HERMES_HOME env.
        dbfile = root_db_path(str(target))
        assert dbfile.exists(), f"index db missing at {dbfile}"
        # when tree-sitter was available the build actually indexed the files
        if site:
            import sqlite3
            conn = sqlite3.connect(str(dbfile))
            try:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM files").fetchone()
                assert rows and rows[0] >= 1, "indexed files landed in the child build"
            finally:
                conn.close()
    finally:
        _cleanup(target, home)


# ---------------------------------------------------------------------------
# 2. a build failure is visible (non-zero exit + traceback), never silent
# ---------------------------------------------------------------------------
def test_entry_failure_is_visible(tmp_path, monkeypatch):
    target = _mk_target("fail")
    # Break the db location: make HERMES_HOME descend from a REGULAR FILE so
    # data_dir()'s mkdir(parents=True) raises -> the build fails at db init.
    anchor = tmp_path / "anchor"
    anchor.write_text("x")
    monkeypatch.setenv("HERMES_HOME", str(anchor / "sub"))
    site = _ts_site()
    if site:
        monkeypatch.setenv("CODE_MAP_PY_SITE", site)
    try:
        res = subprocess.run(
            [sys.executable, str(ENTRY), str(target)],
            capture_output=True, text=True, timeout=120)
        assert res.returncode != 0, "a broken build must exit non-zero"
        assert "Traceback" in res.stderr, (
            "a broken build must print its traceback (fail-visible), got: "
            f"{res.stderr[-300:]!r}")
    finally:
        _cleanup(target, tmp_path)


# ---------------------------------------------------------------------------
# 3. _start_build_process spawns + tracks + reaps a real child
# ---------------------------------------------------------------------------
def test_start_build_process_spawns_tracks_reaps(tmp_path, monkeypatch):
    import threading
    target = _mk_target("spawn")
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    site = _ts_site()
    if site:
        monkeypatch.setenv("CODE_MAP_PY_SITE", site)
    try:
        handle = _state._start_build_process(str(target))
        # it spawned a SUBPROCESS (pid differs from the main process)
        import subprocess as _sp
        assert isinstance(handle, _sp.Popen), "must be a child process, not a thread"
        assert handle.pid != os.getpid(), "the build must run in a SEPARATE process"
        # tracked for reaping
        assert handle in _state._live_build_processes
        # let it finish (bounded wait), then the index lands
        assert _state.join_live_build_threads(timeout=120), "in-flight build did not finish in budget"
        assert handle.poll() is not None, "the child should have exited"
        # reaped + pruned from the live list
        assert handle not in _state._live_build_processes
        assert root_db_path(str(target)).exists(), "index db landed after the child build"
    finally:
        _cleanup(target, home)


# ---------------------------------------------------------------------------
# 4. spawn hiccup -> in-process thread fallback (build never lost)
# ---------------------------------------------------------------------------
def test_spawn_hiccup_falls_back_to_thread(tmp_path, monkeypatch):
    import threading
    target = _mk_target("fallback")
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    site = _ts_site()
    if site:
        monkeypatch.setenv("CODE_MAP_PY_SITE", site)

    def _raise(*a, **k):
        raise OSError("spawn refused (simulated)")

    monkeypatch.setattr(_state.subprocess, "Popen", _raise)
    try:
        handle = _state._start_build_process(str(target))
        # Popen raised -> we fell back to the IN-PROCESS daemon thread
        assert isinstance(handle, threading.Thread), (
            "a spawn hiccup must fall back to the in-process thread, not lose the build")
        assert _state.join_live_build_threads(timeout=120)
    finally:
        _cleanup(target, home)
