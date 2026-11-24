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
    # CRITICAL (P0): the host REPLACES the result with the first string the
    # transform hook returns, so the returned text MUST still carry the
    # original file content — otherwise the model loses the file entirely.
    # The attach block is prepended, the original result body is preserved.
    orig = json.dumps({"content": "1|import os", "total_lines": 2}, ensure_ascii=False)
    full = hook._maybe_nudge("read_file", {"path": str(p)}, result=orig)
    assert full is not None
    assert orig in full, "original read_file result must be preserved inside the returned text"
    assert full.startswith("[code_map]"), f"attach block comes first: {full[:60]!r}"
    assert "func_0" in full and "import" in full
    from code_map.config import READ_SYMBOL_MAX_BYTES
    attach_only = full[: full.index(orig)]
    assert len(attach_only.encode("utf-8")) <= READ_SYMBOL_MAX_BYTES + 40, \
        f"attach block must be byte-capped, got {len(attach_only.encode('utf-8'))} bytes"


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


def test_attach_without_result_body_does_not_lose_the_map(tmp_path):
    """Unit-test / legacy callers pass no `result`: the attach block is
    still returned on its own (no crash, no phantom empty-body marker)."""
    from code_map import hook
    target = _seed_confirmed(tmp_path, ["x.py"], "def x():\n    pass\n")
    p = Path(target) / "x.py"
    out = hook._maybe_nudge("read_file", {"path": str(p)}, result=None)
    assert out is not None
    assert out.startswith("[code_map]")
    assert "x.py" in out
    # No phantom empty-body marker appended.
    assert not out.endswith("\n\n")


def test_binary_read_under_confirmed_target_no_footer(tmp_path):
    """A source extension (.py) whose content is binary must also get NO
    footer: binary sniff short-circuits the attach path before any
    symbol query (binary .py is unparseable; sniffing avoids wasted db
    work)."""
    from code_map import hook, rootreg
    target = _seed_confirmed(tmp_path, ["bin.py"], "def bin(): pass\n")
    bin_p = Path(target) / "bin.py"
    bin_p.write_bytes(b"\x00\x01\x02\x03\x04" * 10)  # NUL bytes in first 512
    out = hook._maybe_nudge("read_file", {"path": str(bin_p)})
    assert out is None, "binary read under a confirmed target must return None"


def test_non_source_read_under_confirmed_target_no_footer(tmp_path):
    """Owner ruling 2026-10-15 (option A): a .md read under a confirmed
    target gets NO footer at all (was: plain directory nudge, which
    hijacked the read result)."""
    from code_map import hook, rootreg
    target = _seed_confirmed(tmp_path, ["d.py"], "def d(): pass\n")
    md = Path(target) / "notes.md"
    md.write_text("# doc\n", encoding="utf-8")
    out = hook._maybe_nudge("read_file", {"path": str(md)})
    assert out is None, "non-source read under a confirmed target must return None"


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


def test_search_files_dir_root_under_confirmed_gets_pointer(tmp_path):
    """The common gap: a `search_files` whose `path` (search root) is a
    DIRECTORY under a confirmed target (e.g. globbing `*emo*` under
    /root/.hermes/plugins/emo). Today this returns None, so the model runs a
    whole-tree glob with zero code_map awareness. New behavior: attach a
    one-line [code_map] POINTER (byte-capped, the follow-up keyword
    `code_map_query` must survive the cap) pointing at the confirmed target —
    the POINT-not-attach discipline (no 30-symbol block on a directory)."""
    from code_map import hook
    target = _seed_confirmed(tmp_path, ["i.py"], "def i(): pass\n")
    out = hook._maybe_nudge("search_files", {"path": target})
    assert out is not None
    assert out.startswith("[code_map]")
    # It must be a pointer to the target, not a symbol attach: name the
    # confirmed root and carry the follow-up keyword `code_map_query`.
    assert target in out
    assert "code_map_query" in out
    # P3-3: pin the FULL pointer text (nudge copy) — the legacy assertion
    # only checked prefix/keyword/cap, so a rewrite of the nudge copy would
    # silently pass.
    expected = (f"[code_map] your search root {target} is under confirmed "
                f"target {target}; for symbol lookup in this project "
                f"prefer code_map_query(root={target}, name=...) over "
                f"whole-tree glob/grep")
    assert out.splitlines()[0] == expected
    # Byte cap respected (the keyword must not be the truncated tail).
    from code_map.config import SEARCH_DIR_NUDGE_MAX_BYTES
    assert len(out.encode("utf-8")) <= SEARCH_DIR_NUDGE_MAX_BYTES + 64


def test_search_files_dir_root_outside_confirmed_no_pointer(tmp_path):
    """A directory search-root that is NOT under any confirmed target stays
    None (no pointer, no crash) — mirrors the read-side out-of-scope rule."""
    from code_map import hook
    target = _seed_confirmed(tmp_path, ["i.py"], "def i(): pass\n")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert hook._maybe_nudge("search_files", {"path": str(outside)}) is None


def test_search_files_bare_pattern_still_none(tmp_path):
    """F6 invariant: a BARE relative pattern (no absolute `path` arg) abspaths
    into the CWD and must stay None even when a confirmed target exists —
    the directory pointer only fires on an absolute search-root path."""
    from code_map import hook
    _seed_confirmed(tmp_path, ["i.py"], "def i(): pass\n")
    assert hook._maybe_nudge("search_files", {"pattern": "i.py"}) is None


# ── symlink-profile regression (owner 2026-10-15, code_map audit) ───────────
# The profile/symlink deployment model: a profile-local dir
# (<home>/profiles/<p>/plugins/code_map) is a SYMLINK to the shared physical
# dir (<home>/plugins/code_map). A read through the profile symlink used to
# be matched with abspath (which keeps the symlink prefix) against a registry
# that stores the shared physical path, so it never matched, was treated as
# "outside any confirmed target", and got a spurious un-indexed-read ledger
# row (which then drove a pre_llm_call "要 index 吗" prompt even though the
# shared target was already confirmed). _find_confirmed_root now normalizes
# both sides via realpath, so the profile-symlink read resolves to the shared
# physical dir and matches.

def test_symlink_profile_read_matches_shared_confirmed_root(tmp_path):
    """A read through a profile-local SYMLINK to a confirmed shared target
    must be recognized as in-scope (attach fires) AND must NOT log a
    spurious un-indexed-read ledger row."""
    import os as _os
    from code_map import hook, rootreg
    # Shared physical target (confirmed + built + not dirty).
    target = _seed_confirmed(tmp_path, ["mod.py"], "def f():\n    return 1\n")
    # Build a sibling profile-style symlink pointing at the shared target.
    symlink_parent = Path(target).parent / "profile_view"
    symlink_parent.mkdir(parents=True, exist_ok=True)
    symlink = symlink_parent / "code_map"
    symlink.symlink_to(Path(target), target_is_directory=True)

    # The read path the model actually uses: the SYMLINK view of mod.py.
    read_path = str(symlink / "mod.py")
    # Sanity: os.path.abspath keeps the symlink prefix (the old bug trigger).
    assert _os.path.abspath(read_path) != _os.path.realpath(read_path)

    # (1) The matched confirmed root resolves to the shared physical target.
    confirmed = set(rootreg.confirmed_targets())
    matched = hook._find_confirmed_root(read_path, confirmed)
    assert matched is not None, "symlink-profile read must match the shared target"
    assert _os.path.realpath(matched) == _os.path.realpath(target)

    # (2) _maybe_nudge attaches (in-scope), does not treat it as out-of-scope.
    out = hook._maybe_nudge("read_file", {"path": read_path})
    assert out is not None and out.startswith("[code_map]"), \
        f"symlink-profile read must auto-attach, got: {out!r}"

    # (3) No spurious ledger row for the profile-symlink dir (the read is
    # recognized as under a confirmed target, not "un-indexed").
    from code_map.config import data_dir
    ledger = data_dir() / "_unindexed_read_ledger"
    lines = []
    if ledger.exists():
        lines = [l for l in ledger.read_text().splitlines() if l.strip()]
    # A row keyed on the profile-symlink dir would be the bug's signature.
    symlink_view = _os.path.realpath(symlink_parent)  # physical, for a fair key
    bad = [l for l in lines if l.startswith(f"{symlink_view}\t")]
    assert not bad, f"symlink-profile read must NOT log an un-indexed row: {bad!r}"


# ── un-indexed project: directory-triggered, not source-file-triggered ─────
# Owner 2026-10-18 (defect 2): reading ANY file (not just source) under an
# un-indexed project's directory must log a ledger row, so the turn-boundary
# clarify can offer to index that project. The legacy extension filter
# (INDEXED_EXTENSIONS only) meant a project whose files the model read were
# .md/.json/etc would NEVER be offered an index.
#
# Two guards keep this honest:
#   (a) a project boundary that lands under a BLOCKED_ROOT_PREFIXES system
#       dir (/usr, /etc, /opt, /home, or bare /) is NOT logged — reading
#       /etc/hostname must not offer to index the whole /etc tree.
#   (b) a read already under a CONFIRMED target is never logged (the
#       attach path, not the pending path).

def test_unindexed_read_non_source_file_logs_ledger_row(tmp_path):
    """Reading a NON-source file (.md) inside an un-indexed project dir
    must log a ledger row for that project dir (the clarify offer is
    directory-triggered, not source-file-triggered).

    Uses tmp_path (pytest's per-test dir, under /tmp or the host's
    TMPDIR) instead of /var/tmp: §2.21 demoted /var to the SILENT
    prefix (is_silent_tree), and _purge_obsolete_rows (which runs
    AFTER _log_unindexed_read in _post_tool_call) calls
    _needs_index_build_question → is_silent_tree → False →
    _clear_project_rows, wiping the row this test asserts on.
    tmp_path carries no silent/system/transient component, so the
    purge keeps the row and the test exercises the real ledger path.
    """
    from code_map import hook
    from code_map.config import data_dir
    # A brand-new project dir under /tmp (non-transient, non-silent,
    # non-system): the host's TMPDIR is /root/.hermes/cache/scratch,
    # which is a transient tree where _needs_index_build_question's
    # is_transient_dir guard vetoes the ledger row. /tmp is a real
    # path outside any confirmed target / marker tree.
    import uuid, shutil
    proj = Path("/tmp") / f"cm_nonsrc_{uuid.uuid4().hex[:8]}"
    _seed_bases.append(proj)
    try:
        (proj / "docs").mkdir(parents=True)
        md = proj / "docs" / "readme.md"
        md.write_text("# hi\n", encoding="utf-8")
        # Session-scoped: _post_tool_call carries the session_id kwarg.
        hook._post_tool_call(
            tool_name="read_file",
            args={"path": str(md)},
            result="", status="ok",
            session_id="sess_test_nonsrc",
        )
        ledger = data_dir() / "_unindexed_read_ledger"
        lines = [l for l in (ledger.read_text().splitlines()
                              if ledger.exists() else []) if l.strip()]
        # _project_boundary keys on the file's DIRECT PARENT DIR (the
        # honest boundary when no root marker is found): docs/, not proj.
        expected = f"{proj / 'docs'}\tsess_test_nonsrc"
        assert expected in lines, \
            f"non-source read under un-indexed project must log its dir row: {lines!r}"
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        _seed_bases.pop()


def test_unindexed_read_system_dir_not_logged(tmp_path):
    """Reading a file whose project boundary is a real system tree (/usr,
    /etc, /opt — the gate-⑩ incident shape) must NOT log a ledger row.
    (/root and /home are deliberately NOT guarded: owner's projects under
    them ARE the indexable case, and a real project resolves to its own
    marker dir via detect_root, not to /root itself.)"""
    from code_map import hook
    from code_map.config import data_dir
    # /etc/hostname always exists on Linux; its _project_boundary is /etc.
    hook._post_tool_call(
        tool_name="read_file",
        args={"path": "/etc/hostname"},
        result="", status="ok",
        session_id="sess_test_sysdir",
    )
    ledger = data_dir() / "_unindexed_read_ledger"
    lines = [l for l in (ledger.read_text().splitlines()
                          if ledger.exists() else []) if l.strip()]
    bad = [l for l in lines if l.split("\t")[0] in ("/etc", "/usr", "/opt")]
    assert not bad, f"system-tree read must NOT log a ledger row: {bad!r}"


# ── terminal 档 2 不对称版 (owner 2026-10-15) ─────────────────────────────────
# Write-side: terminal command with a write signal + workdir under a confirmed
# target  ->  mark_dirty(target).  No per-file reindex (command text can't be
# trusted to name which files changed).
# Read-side: single-file `cat <source.py>` under a confirmed target  ->
# one-line pointer appended to terminal stdout.  Multi-file / pipe / grep
#  ->  no nudge (opaque stdout; we only point, never stuff symbols in).

def test_terminal_write_signal_marks_dirty(tmp_path):
    """A SUCCESSFUL terminal command that carries a write signal and whose
    workdir is inside a confirmed target must mark that target dirty."""
    from code_map import hook, rootreg
    target = _seed_confirmed(tmp_path, ["f.py"], "def f(): pass\n")
    rootreg.clear_dirty(target)
    assert not rootreg.is_dirty(target)
    hook._post_tool_call(
        tool_name="terminal",
        args={"command": "echo x >> /var/tmp/x/f.py", "workdir": target},
        result="", status="ok",
    )
    assert rootreg.is_dirty(target), "write-signal terminal under target must mark dirty"


def test_terminal_no_write_signal_no_dirty(tmp_path):
    """A terminal command WITHOUT a write signal (ls, git status, grep)
    must NOT mark the target dirty — only write-shaped commands do."""
    from code_map import hook, rootreg
    target = _seed_confirmed(tmp_path, ["f.py"], "def f(): pass\n")
    rootreg.clear_dirty(target)
    for cmd in ("ls", "git status", "grep -n f f.py", "python -m pytest"):
        hook._post_tool_call(
            tool_name="terminal",
            args={"command": cmd, "workdir": target},
            result="", status="ok",
        )
    assert not rootreg.is_dirty(target), f"non-write terminal must not dirty: got dirty"


def test_terminal_quoted_read_commands_no_false_dirty(tmp_path):
    """REGRESSION (reviewer 2026-10-15 HIGH): read-only terminal commands that
    MERELY MENTION a write token inside a QUOTED ARGUMENT must NOT mark the
    target dirty. The sniff is quote-aware: `grep -rn "rm " .` and
    `grep "a > b" f.py` are read-only even though they contain `rm` / `>`.
    A real write outside quotes MUST still fire (the write-side signal is
    not suppressed by the quote fix)."""
    from code_map import hook, rootreg
    target = _seed_confirmed(tmp_path, ["f.py"], "def f(): pass\n")
    rootreg.clear_dirty(target)
    # Read-only commands where the write-ish token sits inside quotes.
    for cmd in ('grep -rn "rm " .', 'grep "a > b" f.py', 'echo "rm -rf x"'):
        hook._post_tool_call(
            tool_name="terminal",
            args={"command": cmd, "workdir": target},
            result="", status="ok",
        )
        assert not rootreg.is_dirty(target), \
            f"quoted read-only command must not dirty: {cmd!r}"
    # A REAL redirect outside quotes must still mark dirty.
    hook._post_tool_call(
        tool_name="terminal",
        args={"command": "echo x > f.py", "workdir": target},
        result="", status="ok",
    )
    assert rootreg.is_dirty(target), "real out-of-quote redirect must dirty"


def test_terminal_write_outside_target_no_dirty(tmp_path):
    """A write-signal command whose workdir is OUTSIDE every confirmed target
    must not touch the registry (fail-soft no-op)."""
    from code_map import hook, rootreg
    target = _seed_confirmed(tmp_path, ["f.py"], "def f(): pass\n")
    rootreg.clear_dirty(target)
    hook._post_tool_call(
        tool_name="terminal",
        args={"command": "echo x > /tmp/outside.py", "workdir": "/tmp"},
        result="", status="ok",
    )
    assert not rootreg.is_dirty(target), "out-of-target write must not dirty the target"


def test_terminal_failed_write_no_dirty(tmp_path):
    """件 1 discipline: a FAILED terminal command (status=error) has not
    mutated anything we know about -> no mark_dirty (db keeps prior state)."""
    from code_map import hook, rootreg
    target = _seed_confirmed(tmp_path, ["f.py"], "def f(): pass\n")
    rootreg.clear_dirty(target)
    hook._post_tool_call(
        tool_name="terminal",
        args={"command": "echo x >> /var/tmp/x/f.py", "workdir": target},
        result="", status="error",
    )
    assert not rootreg.is_dirty(target), "failed terminal write must not dirty"


def test_terminal_read_cat_appends_pointer(tmp_path):
    """A single-file `cat <source.py>` of a confirmed-target source file
    appends a one-line code_map_query pointer to the terminal stdout
    (the host's _with_body prepends the pointer AFTER the original body)."""
    from code_map import hook
    target = _seed_confirmed(tmp_path, ["mod.py"], "def mod(): pass\n")
    import os
    mod = os.path.join(target, "mod.py")
    # _maybe_nudge is the read-side dispatcher; on_result routes to it.
    result = hook.on_result(
        tool_name="terminal",
        args={"command": f"cat {mod}", "workdir": target},
        result="stdout-content",
    )
    assert result is not None, "cat of confirmed-target source must produce a pointer"
    assert "code_map_query" in result, f"pointer must name code_map_query: {result!r}"
    # P0 preservation: the original terminal stdout must still be present.
    assert "stdout-content" in result, "terminal stdout must survive (P0 preservation)"


def test_terminal_read_pipe_no_nudge(tmp_path):
    """A piped / multi-file / grep-style terminal read must NOT get a
    pointer (we refuse to point at opaque multi-source stdout)."""
    from code_map import hook
    target = _seed_confirmed(tmp_path, ["a.py", "b.py"], "def a(): pass\ndef b(): pass\n")
    import os
    a, b = os.path.join(target, "a.py"), os.path.join(target, "b.py")
    for cmd in (
        f"cat {a} | wc -l",
        f"cat {a} {b}",
        f"grep -n def {a}",
    ):
        out = hook.on_result(tool_name="terminal",
                             args={"command": cmd, "workdir": target},
                             result="x")
        assert out is None, f"opaque terminal read must not nudge: {cmd!r} -> {out!r}"


# ── D-never-ask-persist (AUDIT §2.16 alignment) ─────────────────────────

def test_never_ask_persist_blocks_new_session(tmp_path):
    """A '不再问' settle must write the store-level _never_ask marker
    so that a NEW session's re-read of the same project does NOT
    re-log a ledger row (the Go D-never-ask-persist ruling, AUDIT
    §2.16, 2026-11-21: the reference's row-deletion-only P2
    simplification re-asked on a new-session re-read; the owner
    requires the choice to persist).

    Uses /tmp (non-transient, non-silent, non-system) for the
    fixture project dir, same rationale as
    test_unindexed_read_non_source_file_logs_ledger_row above.
    """
    from code_map import hook
    from code_map.config import data_dir, never_ask_file, path_key
    import uuid, shutil

    proj = Path("/tmp") / f"cm_neverask_{uuid.uuid4().hex[:8]}"
    _seed_bases.append(proj)
    try:
        (proj / "docs").mkdir(parents=True)
        md = proj / "docs" / "readme.md"
        md.write_text("# hi\n", encoding="utf-8")

        # 1. Session A reads the project → logs a ledger row.
        hook._post_tool_call(
            tool_name="read_file",
            args={"path": str(md)},
            result="", status="ok",
            session_id="sess_A",
        )
        ledger = data_dir() / "_unindexed_read_ledger"
        assert ledger.exists(), "session A's read must log a ledger row"
        a_lines = [l for l in ledger.read_text().splitlines() if l.strip()]
        assert any("sess_A" in l for l in a_lines), \
            f"session A row missing: {a_lines!r}"

        # 2. Session A settles '不再问' → clears the ledger row AND
        #    writes the _never_ask marker.
        hook._settle_clarify_answer(
            args={"questions": [{"question": f"要不要为 {proj} 建 code_map 索引？"}]},
            result={"responses": [{"status": "answered", "user_response": "不再问"}]},
            status="ok",
            session_id="sess_A",
        )
        na = never_ask_file()
        assert na.exists(), "_never_ask marker file must exist after settle"
        na_lines = [l for l in na.read_text().splitlines() if l.strip()]
        proj_key = path_key(str(proj))
        assert proj_key in na_lines, \
            f"project key {proj_key!r} missing from _never_ask: {na_lines!r}"

        # 3. Session B (a NEW session) re-reads the same project →
        #    the _never_ask guard blocks the re-log.
        hook._post_tool_call(
            tool_name="read_file",
            args={"path": str(md)},
            result="", status="ok",
            session_id="sess_B",
        )
        if ledger.exists():
            b_lines = [l for l in ledger.read_text().splitlines() if l.strip()]
            assert not any("sess_B" in l for l in b_lines), \
                f"session B must NOT re-log a _never_ask-marked project: {b_lines!r}"
    finally:
        shutil.rmtree(proj, ignore_errors=True)
        _seed_bases.pop()

