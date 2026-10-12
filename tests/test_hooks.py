"""Hook behavior tests: 件1 success-gate, 件2 attach, 件3 bounded self-heal
(reviewer F1/F2 hardening), byte-cap exactness (F4), no double-reindex (G),
search_files path (F6).

All tests point HERMES_HOME at a tmp dir so the code_map data dir (registry,
dirty flags, heal attempts, per-root dbs) is fully isolated from the live
production data.
"""
import os
import json
import shutil
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # 自适应 bootstrap: plugin 根
import pytest

# Unified sys.path bootstrap via the package __init__ (works from any
# checkout location — the real profile path, a git worktree, anywhere):

# e-1 hygiene: per-test /var/tmp/cm_hooks_* seed bases, cleaned up by the
# autouse fixture's teardown (one base registered per test, LIFO == that
# test's base).
_seed_bases: list[Path] = []


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch, request):
    """Every hook test runs against a fresh, isolated code_map data dir so
    production _registry/_dirty/dbs are never touched (and never leaked)."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Clear module-level caches that may have been built against a prior
    # HERMES_HOME (data_dir() is called lazily in rootreg helpers).
    yield home
    # e-1 hygiene: drop this test's /var/tmp/cm_hooks_* seed dir. Each test
    # registers at most one base via _seed_confirmed, so LIFO pop is exact.
    if _seed_bases:
        base = _seed_bases.pop()
        shutil.rmtree(base, ignore_errors=True)


def _make_store(root: Path, filenames, contents):
    """Create `filenames` (relative paths) with `contents` under root.

    `contents` may be a parallel list (one text per file) or a single string
    for the single-file case (the common call shape in this suite).
    """
    if isinstance(contents, str):
        contents = [contents] * len(filenames)
    for rel, text in zip(filenames, contents):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _seed_confirmed(tmp_path, source_files, source_texts):
    """Build a small confirmed target and register it.

    The target lives at /var/tmp/cm_hooks_<uuid> — deliberately OUTSIDE the
    pytest TMPDIR: in this env TMPDIR is redirected under the coder profile's
    scratch dir, which is itself under a confirmed code_map target, and an
    index walk rooted there inherits scratch residue into the seed db.
    The uuid keeps the base unique per test within one run (pytest
    truncates tmp_path names to ~30 chars, which collides). The registry
    entry lives in the per-test HERMES_HOME (isolated fixture), so it dies
    with the test. The /var/tmp seed dir is registered in `_seed_bases`
    and cleaned up by the autouse fixture's teardown (e-1 hygiene — a prior
    version of this helper leaked one cm_hooks_* dir per test into /var/tmp).
    Returns the target path string.

    `tmp_path` is unused for the target (kept in the signature for
    fixture-style consistency with the other seeds in this suite).
    """
    import uuid
    base = Path("/var/tmp") / f"cm_hooks_{uuid.uuid4().hex[:12]}"
    _seed_bases.append(base)
    target = base / "proj"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    _make_store(target, source_files, source_texts)
    # Seed the db + registry via the real builder (deterministic, zero LLM).
    from code_map import rootreg
    rootreg.write_confirmed_root(str(target))
    from code_map.state import _run_build
    _run_build(str(target))
    # 件1 side-effect discipline: a fresh build leaves the target usable and
    # NOT dirty — the write hook is the only thing that stamps dirty.
    rootreg.clear_dirty(str(target))
    # Invalidate the db query cache so a later read-side attach sees the
    # freshly-built rows (the build may have left a stale cached on-disk set).
    from code_map.fingerprint import invalidate_cache
    invalidate_cache(str(target))
    return str(target)


# ── 件 1: success gate ───────────────────────────────────────────────────────

def test_post_tool_call_reindexes_on_success_skips_on_error(tmp_path):
    from code_map import hook, rootreg
    target = _seed_confirmed(tmp_path, ["a.py"], "def hello():\n    pass\n")
    src = Path(target) / "a.py"
    # Simulate a SUCCESSFUL write (host passes status='ok').
    src.write_text("def hello():\n    return 1\n",
                                             encoding="utf-8")
    hook._post_tool_call(tool_name="write_file", args={"path": str(src)},
                         result={}, status="ok")
    assert rootreg.is_dirty(target), "successful write must mark the target dirty"
    # Simulate a FAILED write (status='error') — must NOT reindex / re-mark.
    rootreg.clear_dirty(target)
    hook._post_tool_call(tool_name="write_file", args={"path": str(src)},
                         result={"error": "boom"}, status="error")
    assert not rootreg.is_dirty(target), "failed write must NOT mark dirty"
    # A write OUTSIDE any confirmed target is ignored.
    outside = tmp_path / "outside.py"
    outside.write_text("def x(): pass\n", encoding="utf-8")
    hook._post_tool_call(tool_name="write_file", args={"path": str(outside)},
                         result={}, status="ok")
    assert not rootreg.is_dirty(str(outside))


def test_post_tool_call_no_double_reindex_with_transform(tmp_path):
    """Host fires BOTH hooks for one write; only post_tool_call reindexes
    (on_result no-ops write tools) — exactly ONE db mutation (reviewer G)."""
    from code_map import hook, rootreg
    target = _seed_confirmed(tmp_path, ["b.py"], "def f():\n    pass\n")
    src = Path(target) / "b.py"
    src.write_text("def f():\n    return 2\n",
                                             encoding="utf-8")
    # transform_tool_result (on_result) for a write tool -> None, no reindex.
    assert hook.on_result("write_file", {"path": str(src)}, result={}) is None
    # post_tool_call -> reindex + dirty.
    hook._post_tool_call("write_file", {"path": str(src)}, result={}, status="ok")
    assert rootreg.is_dirty(target)
    # Fire on_result AGAIN for the same write — still no-op, no second mark.
    assert hook.on_result("write_file", {"path": str(src)}, result={}) is None
    # db row count for b.py must be exactly 1 file entry (no dup).
    import sqlite3
    from code_map.config import root_db_path
    c = sqlite3.connect(str(root_db_path(target)))
    n = c.execute("SELECT COUNT(*) FROM files WHERE path=?", (str(src),)).fetchone()[0]
    c.close()
    assert n == 1, f"write must yield exactly one files row, got {n}"


# ── 件 2: attach correctness + byte cap ─────────────────────────────────────

def test_attach_returns_symbol_outline_for_source_file(tmp_path):
    from code_map import hook, rootreg
    target = _seed_confirmed(
        tmp_path,
        ["mod.py"],
        "import os\nimport re\n\n" + "\n".join(
            f"def func_{i}():\n    return {i}\n" for i in range(40)) + "\n")
    p = Path(target) / "mod.py"
    out = hook._maybe_nudge("read_file", {"path": str(p)})
    assert out is not None, "source-file read under a confirmed target must attach"
    assert out.startswith("[code_map]"), f"attach must be the code_map block: {out[:60]!r}"
    assert "func_0" in out and "import" in out
    from code_map.config import READ_SYMBOL_MAX_BYTES
    assert len(out.encode("utf-8")) <= READ_SYMBOL_MAX_BYTES + 40, \
        f"attach must be byte-capped, got {len(out.encode('utf-8'))} bytes"


def test_attach_byte_cap_is_exact_not_char_based(tmp_path):
    """Reviewer F4: the cap slices on the ENCODED form. Build a payload that
    would blow the cap via wide CJK signatures, assert the returned text is
    <= cap + marker and is valid UTF-8."""
    from code_map import hook, rootreg
    from code_map.config import READ_SYMBOL_MAX_BYTES
    # CJK-heavy signatures: each line is multi-byte, forces the byte-cap path.
    funcs = "\n".join(
        f"def 函数_{i}():\n    return '符号说明文本说明' * {i}\n"
        for i in range(35))
    target = _seed_confirmed(tmp_path, ["cjk.py"], "import os\n" + funcs)
    p = Path(target) / "cjk.py"
    out = hook._maybe_nudge("read_file", {"path": str(p)})
    assert out is not None
    b = out.encode("utf-8")
    # Must be a clean UTF-8 boundary (decode round-trips without error).
    out.encode("utf-8").decode("utf-8")
    assert len(b) <= READ_SYMBOL_MAX_BYTES + 40, \
        f"CJK attach overflowed cap: {len(b)} bytes > {READ_SYMBOL_MAX_BYTES + 40}"


def test_non_source_read_gets_plain_nudge_not_attach(tmp_path):
    from code_map import hook, rootreg
    target = _seed_confirmed(tmp_path, ["d.py"], "def d(): pass\n")
    md = Path(target) / "notes.md"
    md.write_text("# doc\n", encoding="utf-8")
    out = hook._maybe_nudge("read_file", {"path": str(md)})
    # .md is not in INDEXED_EXTENSIONS -> plain directory nudge, no symbol block.
    assert out is not None and out.startswith("[code_map]")
    assert "symbol(s) in this file" not in out, "non-source must NOT attach symbols"


def test_read_outside_confirmed_target_no_footer(tmp_path):
    """Owner design (2026-10-13, turn-boundary clarify): a read_file that
    lands on a SOURCE file outside any confirmed target returns None from
    the transform_tool_result face (the per-read mid-turn footer that
    "抢跑" was RETIRED — it was bypassed by a model switch to
    terminal/execute_code and fired before the user ever got to respond).
    The clarify-trigger role moved to the pre_llm_call face: this same
    read, via post_tool_call, logs the file's enclosing DIRECTORY into the
    session ledger, and the NEXT turn's pre_llm_call notice is where the
    "ask the user 要/不要/不再问" question is delivered. See
    tests/test_turn_boundary_clarify.py for that face.

    Note: `_seed_confirmed` registers a confirmed target for this test
    (proves the other read-side paths still work), but the 'other/e.py'
    file below lives in a fresh tmp_path that is NOT that target — so
    it genuinely falls into the `enclosing is None` branch we're testing.
    """
    from code_map import hook, rootreg
    _seed_confirmed(tmp_path, ["e.py"], "def e(): pass\n")
    outside = tmp_path / "other" / "e.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("def e(): pass\n", encoding="utf-8")
    # Sanity: 'other' is NOT under any confirmed target.
    assert hook._find_confirmed_root(str(outside), set(rootreg.confirmed_targets())) is None
    out = hook._maybe_nudge("read_file", {"path": str(outside)})
    # (behavior change, owner-signed 2026-10-13): was `assert out is not None`
    assert out is None, "out-of-scope read must NOT carry a per-read footer anymore"


def test_read_non_source_outside_confirmed_target_still_none(tmp_path):
    """A NON-source file (e.g. .md) read outside any confirmed target must
    still return None — the footer is only for source files the index
    could actually cover."""
    from code_map import hook
    _seed_confirmed(tmp_path, ["e.py"], "def e(): pass\n")
    md = tmp_path / "other" / "notes.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("# hi\n", encoding="utf-8")
    assert hook._maybe_nudge("read_file", {"path": str(md)}) is None


def test_read_system_dir_file_outside_confirmed_target_no_footer(tmp_path):
    """Owner design (2026-10-13, turn-boundary clarify): a source file that
    literally sits at a BLOCKED root prefix (/etc/...) returns None from
    the read face (the per-read footer is gone). The /etc read's ledger
    write happens via post_tool_call, and the next turn's pre_llm_call
    notice names /etc as the proposed boundary — but the BUILD decision
    for /etc (whether the Q3 gate permits it) is still the gate's call,
    never the read face's. See test_turn_boundary_clarify.py."""
    from code_map import hook
    sysfile = "/etc/some_module.py"
    out = hook._maybe_nudge("read_file", {"path": sysfile})
    assert out is None, "out-of-scope read (incl. /etc) must not carry a footer"


def test_footer_boundary_is_honest_smallest_containing_dir(tmp_path):
    """Owner design (2026-10-13, turn-boundary clarify): the honest-smallest
    boundary logic moved OUT of the read face into the ledger's
    `_project_boundary` helper (post_tool_call side) — the SAME rule the
    retired footer used: detect_root's marker result only when it actually
    encloses the file, else the file's immediate parent dir. Test the
    helper directly: an out-of-home /usr path with no marker must land on
    the file's own parent dir, NOT detect_root's last-resort $HOME."""
    from code_map import hook
    target = "/usr/local/lib/hermes_plugin/stub.py"
    boundary = hook._project_boundary(target)
    assert boundary == "/usr/local/lib/hermes_plugin", (
        f"boundary must be the file's own parent dir, got: {boundary}")


# ── 件 3: bounded self-heal (reviewer F1/F2) ────────────────────────────────

def test_heal_skips_full_rebuild_above_file_threshold(tmp_path):
    """A target with > READ_HEAL_MAX_FILES indexed files must NOT trigger a
    full _run_build on the read path (the BLOCKER); instead it clears the
    dirty stamp and attaches from the stale db."""
    from code_map import hook, rootreg
    from code_map.config import READ_HEAL_MAX_FILES
    # Make a target whose INDEXED count exceeds the threshold WITHOUT building
    # READ_HEAL_MAX_FILES real files: patch _count_indexed_files to report a
    # large number, then assert _run_build is NOT called.
    target = _seed_confirmed(tmp_path, ["f.py"], "def f(): pass\n")
    rootreg.mark_dirty(target)
    calls = {"n": 0}
    real_build = None
    import code_map.state as st
    real_build = st._run_build
    def fake_build(root):
        calls["n"] += 1
        real_build(root)
    # Patch hook's import path: hook does `from .state import _run_build` inside
    # _heal_dirty, so we patch the attribute on the state module.
    st._run_build = fake_build
    try:
        # Force the size guard to trip.
        import code_map.hook as hk
        real_count = hk._count_indexed_files
        hk._count_indexed_files = lambda root: READ_HEAL_MAX_FILES + 1
        try:
            out = hook._maybe_nudge("read_file", {"path": str(Path(target) / "f.py")})
        finally:
            hk._count_indexed_files = real_count
    finally:
        st._run_build = real_build
    assert calls["n"] == 0, "large target must NOT run a full _run_build on the read path"
    assert not rootreg.is_dirty(target), "size guard must clear the dirty stamp"
    # Still attaches from the (stale but present) db.
    assert out is not None and out.startswith("[code_map]")


def test_heal_gives_up_after_max_failed_attempts(tmp_path, monkeypatch):
    """A persistently failing rebuild must stop after READ_HEAL_MAX_ATTEMPTS
    (reviewer F2) — no unbounded retry on every read."""
    from code_map import hook, rootreg
    from code_map.config import READ_HEAL_MAX_ATTEMPTS
    import code_map.state as st
    target = _seed_confirmed(tmp_path, ["g.py"], "def g(): pass\n")
    rootreg.mark_dirty(target)
    real_build = st._run_build
    st._run_build = lambda root: (_ for _ in ()).throw(RuntimeError("db locked"))
    try:
        key = os.path.abspath(target)
        # First attempt: heal tries, fails, bumps counter to 1.
        hook._heal_dirty(target)
        assert rootreg.heal_attempts(key) == 1, \
            "a failed heal must bump the attempt counter"
        # Force the counter to the cap and verify no further _run_build call.
        while rootreg.heal_attempts(key) < READ_HEAL_MAX_ATTEMPTS:
            rootreg.note_heal_failure(key)
        assert rootreg.heal_attempts(key) >= READ_HEAL_MAX_ATTEMPTS
        # Now _heal_dirty must return WITHOUT calling _run_build.
        built = {"n": 0}
        def counting(root):
            built["n"] += 1
            real_build(root)
        st._run_build = counting
        hook._heal_dirty(target)
        assert built["n"] == 0, "give-up guard must stop further full rebuilds"
        # Stamp stays dirty (only an explicit refresh clears it post-give-up).
        assert rootreg.is_dirty(target)
    finally:
        st._run_build = real_build


def test_heal_success_clears_dirty_and_resets_attempts(tmp_path):
    from code_map import hook, rootreg
    import code_map.state as st
    target = _seed_confirmed(tmp_path, ["h.py"], "def h(): pass\n")
    rootreg.mark_dirty(target)
    key = os.path.abspath(target)
    rootreg.note_heal_failure(key)  # a prior failed attempt
    hook._heal_dirty(target)  # this one succeeds (real build)
    assert not rootreg.is_dirty(target)
    assert rootreg.heal_attempts(key) == 0, "successful heal must reset the counter"


# ── cross-process write lock (reviewer d-2) ────────────────────────────────

def test_write_lock_serializes_and_is_advisory(tmp_path):
    """fcntl.flock on the shared data dir's _proc.lock: nowait never
    blocks on contention (advisory), the deadline-bounded blocking path
    stays inside the host budget, and re-acquire after release works."""
    from code_map import lockfile
    import fcntl, os as _os, time as _time
    # Create the lock file (write_lock's first use does mkdir+open; touch
    # it here so the contended-fd setup below has a real file).
    lf = lockfile._lock_path()
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.touch()
    # nowait path: acquires when uncontended.
    with lockfile.write_lock(nowait=True):
        pass
    # A contending holder (fd 2nd open, own flock handle — flock locks are
    # per open file description, so this genuinely contends with
    # write_lock's).
    fd = _os.open(str(lf), _os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        # nowait: must return immediately (body still runs — advisory).
        import time as _time
        t0 = _time.monotonic()
        with lockfile.write_lock(nowait=True):
            pass
        assert _time.monotonic() - t0 < 0.2, "nowait must not block on contention"
        # blocking-with-tiny-deadline: must proceed UNLOCKED past the
        # deadline, not wedge the worker.
        t0 = _time.monotonic()
        with lockfile.write_lock(nowait=False, timeout_s=0.1):
            pass
        assert _time.monotonic() - t0 < 1.0, "deadline path must be bounded"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        _os.close(fd)
    # Contended no more: acquiring now succeeds and releases.
    with lockfile.write_lock(nowait=False, timeout_s=0.5):
        pass


# (d-1, reviewer: write-side upsert holds `_heal_lock` + advisory cross-process
#  flock, so an in-process full-build heal cannot interleave its diff snapshot
#  with the upsert. Exercised end-to-end by test_heal_success_* — a heal run
#  while a write-side reindex is in flight is the d-1 scenario; the lock
#  serializes them, and `test_post_tool_call_reindexes_on_success_skips_on_error`
#  proves the write side completes without a concurrent heal wedge.)


# ── search_files behavior (reviewer F6) ─────────────────────────────────────

def test_search_files_pattern_no_false_attach(tmp_path):
    """search_files passes a `pattern` (regex/glob), not a file path. A bare
    pattern abspaths into the CWD and must NOT match a confirmed target —
    no attach, no crash."""
    from code_map import hook, rootreg
    target = _seed_confirmed(tmp_path, ["i.py"], "def i(): pass\n")
    # A bare pattern abspaths into the CWD (not under the target) -> None.
    assert hook._maybe_nudge("search_files", {"pattern": r"def i\(\)"}) is None
    # A pattern that happens to be an absolute path INSIDE a confirmed target
    # still attaches (documented behavior).
    p = Path(target) / "i.py"
    out = hook._maybe_nudge("search_files", {"path": str(p)})
    assert out is not None and out.startswith("[code_map]")
