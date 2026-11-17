"""state.py — async build/refresh lifecycle + root confirmation.

Contract ⑥: build_id (UUID) + pid dual liveness check; zombie resume
≠ full rebuild. Contract ⑦: per-file commit batching every
BUILD_COMMIT_BATCH files.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from typing import Any, Dict, Optional

from .config import BUILD_COMMIT_BATCH, PARSER_GENERATION, data_dir, root_db_path, ROOT_MARKERS
from .db import init_db, insert_symbols, upsert_file, remove_paths
from .fingerprint import file_fingerprint, invalidate_cache, on_disk_files
from .parser import parse_file, available
from .rootdetect import detect_root, find_root_candidates
from . import rootreg

import logging

_log = logging.getLogger("code_map.state")

# Live fire-and-forget build threads, so callers that need to quiesce a
# target before e.g. rmtree'ing its db dir can join them (test hygiene;
# production callers may ignore this list entirely).
_live_build_threads: "list[threading.Thread]" = []
# Process-isolated builds (owner 2026-11-17): fire-and-forget CHILD processes
# running _run_build. A same-process daemon thread soft-competes the main
# process's GIL / CPU / memory for a heavy tree-sitter parse + sqlite commit;
# a child process does not (cross-process sqlite safety is already handled by
# WAL + the lockfile). Tracked the same way _live_build_threads is so
# join_live_build_threads can quiesce both kinds before a dir cleanup.
_live_build_processes: "list[subprocess.Popen]" = []
_live_threads_lock = threading.Lock()


def join_live_build_threads(timeout: Optional[float] = 30.0) -> bool:
    """Wait for all in-flight fire-and-forget builds to finish.

    Covers BOTH the in-process daemon build threads (the heal path / the
    spawn-hiccup fallback) AND the process-isolated build child processes
    (owner 2026-11-17). Returns True if every in-flight build finished within
    `timeout` seconds (or there were none), False if at least one is still
    running past the deadline (the caller then proceeds at its own risk —
    daemon threads / child processes are reaped at interpreter exit, but any
    dir/file cleanup racing them may observe partial/leaked paths)."""
    done = True
    with _live_threads_lock:
        threads = [t for t in _live_build_threads if t.is_alive()]
    for t in threads:
        t.join(timeout)
        if timeout is not None:
            timeout = max(0.0, timeout - 0.1)  # crude budget split
        if t.is_alive():
            done = False
    with _live_threads_lock:
        _live_build_threads[:] = [t for t in _live_build_threads if t.is_alive()]
    # AND reap the process-isolated build children (quiesce before a dir
    # cleanup can rmtree their db dir out from under a still-writing child).
    with _live_threads_lock:
        procs = [p for p in _live_build_processes if p.poll() is None]
    for p in procs:
        try:
            p.wait(timeout=timeout)
        except Exception:
            pass
        if p.poll() is None:
            done = False
    with _live_threads_lock:
        _live_build_processes[:] = [
            p for p in _live_build_processes if p.poll() is None]
    return done


def _build_entry_script() -> str:
    """Path to the standalone build-subprocess entry (lives in the plugin dir,
    run by <this interpreter> build_subprocess_entry.py <target>)."""
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "build_subprocess_entry.py")


def _start_build_process(target: str):
    """Fire-and-forget build in a CHILD process (owner 2026-11-17), NOT a
    same-process daemon thread.

    The heavy full build (_run_build: tree-sitter parse + sqlite commit) runs
    in a child process so it does NOT share the main process's GIL / CPU /
    memory — a same-process thread would soft-contend the main process's
    concurrent work (the user's 'indexing 打断主进程' fix). The child runs
    `build_subprocess_entry.py <target>` under the SAME interpreter
    (sys.executable): it self-bootstraps (import code_map -> fcntl shim +
    ensure_dep_paths splices tree-sitter) and runs _run_build under the
    cross-process write_lock (advisory; sqlite WAL keeps the db correct
    regardless). Exit 0 = built; non-zero + a traceback on the host stderr =
    a visible failure (mirrors _safe_run_build's fail-visible posture).

    The Popen handle is tracked in _live_build_processes (bounded + reaped by
    join_live_build_threads). Fail-soft: if Popen itself fails (no
    interpreter / spawn error), fall back to the IN-PROCESS daemon thread so a
    build is never lost to a spawn hiccup — we just give up the isolation."""
    entry = _build_entry_script()
    try:
        kwargs = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            # inherit the host stderr so a child traceback lands in the
            # gateway log (fail-visible, mirroring _safe_run_build).
            stderr=None,
        )
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen([sys.executable, entry, target], **kwargs)
    except Exception as exc:
        logging.getLogger("code_map.state").warning(
            "code_map build subprocess spawn failed (%s); falling back to an "
            "in-process build thread", exc)
        return _start_build_thread(target)
    with _live_threads_lock:
        _live_build_processes.append(proc)
        # bound: prune already-exited, cap the live list (oldest trimmed)
        _live_build_processes[:] = [
            p for p in _live_build_processes if p.poll() is None][-8:]
    return proc


def _start_build_thread(target: str) -> threading.Thread:
    t = threading.Thread(target=_safe_run_build, args=(target,), daemon=True)
    with _live_threads_lock:
        _live_build_threads.append(t)
    t.start()
    return t


def _safe_run_build(root: str, on_fail: Any = None) -> bool:
    """Thread-safety wrapper around `_run_build`. Returns True iff the
    build succeeded.

    Fire-and-forget build threads (confirm_root / refresh_root) must
    NEVER let an unhandled exception escape the thread — pytest's
    threadexception plugin escalates those to warnings that mask REAL
    failures (the 5-warning test-run incident, 2026-10-11: test cleanup
    rmtrees the db dir while a background build thread is still
    mid-`init_db`, the thread dies unhandled). Catch the whole build,
    log it (so a broken build is VISIBLE, not silent), and, if `on_fail`
    is provided, call it — the heal path uses that to bump its give-up
    counter so a persistently failing target stops hammering the hot
    read path (reviewer F2)."""
    try:
        _run_build(root)
        ok = True
    except Exception as exc:
        _log.warning("code_map build failed for %s: %s", root, exc)
        if on_fail is not None:
            try:
                on_fail(root)
            except Exception:
                pass  # a callback hiccup must not mask the original failure
        ok = False
    finally:
        # Drop ourselves from the live list once we're done, no matter
        # what — otherwise _live_build_threads grows unbounded over the
        # process lifetime (fire-and-forget builds never re-run, so a
        # dead thread's entry would linger forever).
        with _live_threads_lock:
            t = threading.current_thread()
            try:
                _live_build_threads.remove(t)
            except ValueError:
                pass
    return ok


def _gate10(root: str, path: str) -> Optional[Dict[str, Any]]:
    """Gate 10 (owner's rule, 2026-10-11 v2): root AND path are both
    mandatory; the effective target is normpath(join(root, path)).

    Refusal payload when either field is missing/empty, the boundary
    root is the bare filesystem root "/", the path is absolute
    (boundary semantics undefined), or the joined target escapes the
    boundary (../ traversal). Returns None when the pair passes."""
    target = rootreg._join_target(root, path)
    if target is not None:
        return None
    root = (root or "").strip()
    path = (path or "").strip()
    if not root or not path:
        return {
            "root": root or None, "path": path or None, "status": "invalid",
            "reason": ("gate: root AND path are both required "
                       "(a root-only call would index a whole boundary "
                       "dir; a path-only call has no boundary)"),
        }
    if os.path.abspath(root) == os.sep:
        return {
            "root": root, "path": path, "status": "invalid",
            "reason": "gate: the boundary root must not be '/' (whole-filesystem scan)",
        }
    if os.path.isabs(path):
        return {
            "root": root, "path": path, "status": "invalid",
            "reason": ("gate: path must be RELATIVE to root "
                       "(an absolute path defines no boundary)"),
        }
    return {
        "root": root, "path": path, "status": "invalid",
        "reason": "gate: path escapes the boundary root (../ traversal)",
    }


def confirm_root(root_path: str = "", path: str = "") -> Dict[str, Any]:
    """Confirm a (root, path) pair, kick off async build. Non-blocking.

    v2 signature: the tool surface passes root= boundary + path=
    relative sub-path; `root_path` is kept only for the legacy
    single-argument call shape, where it is interpreted as
    root=root_path with path="" — which FAILS the gate (both fields
    mandatory). New callers must supply path explicitly."""
    gate = _gate10(root_path, path)
    if gate is not None:
        return gate
    from . import rootreg
    target = rootreg._join_target(root_path, path)
    assert target is not None  # gate returned None (pass) above
    rootreg.write_confirmed_root(target)
    _start_build_process(target)
    return {"root": target, "path": path, "status": "build_started",
            "build_id": str(uuid.uuid4())}


def refresh_root(root_path: str, path: str = "") -> Dict[str, Any]:
    """Authoritative re-scan of a CONFIRMED target. Non-blocking.

    Accepts the same (root, path) pair shape as confirm_root: the
    joined target must equal a registry entry. A legacy single-path
    call (root_path= full target, path="") is also accepted when the
    target itself is confirmed — the gate rule (both fields mandatory)
    applies to NEW confirms, not to refreshing an already-confirmed
    target that lives in the registry verbatim."""
    if path:
        gate = _gate10(root_path, path)
        if gate is not None:
            return gate
        target = rootreg._join_target(root_path, path)
    else:
        target = os.path.abspath(root_path)
    if not target:
        return {"root": None, "status": "invalid",
                "note": "root required; an empty call is invalid"}
    if not rootreg.is_confirmed_root(target):
        return {"root": target, "status": "invalid",
                "note": "root not confirmed; call code_map_confirm_root first"}
    invalidate_cache(target)
    _start_build_process(target)
    # Enrich the return so the caller can verify the refresh WITHOUT a
    # terminal/log tail: db_path is deterministic from the target, and
    # files_indexed is a cheap sqlite COUNT of the current index state
    # (0 when the db hasn't been built yet — the field is still present,
    # which is what closes the verification loop). Fail-soft: any read
    # error yields 0, never blocks the async refresh.
    from .config import root_db_path
    db_file = root_db_path(target)
    files_indexed = _count_indexed_files(target)
    return {
        "root": target, "status": "refresh_started",
        "db_path": str(db_file), "files_indexed": files_indexed,
    }


def _count_indexed_files(target: str) -> int:
    """Return how many files are currently indexed for `target` in its
    per-root db. Returns 0 when the db does not exist yet or on any read
    error (fail-soft — a refresh is never blocked by the count)."""
    import sqlite3
    from .config import root_db_path
    db = root_db_path(target)
    if not db.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db), timeout=5.0)
        try:
            # files table is per-root (one db per target), so a plain count.
            cnt = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            return int(cnt)
        finally:
            conn.close()
    except Exception:
        return 0


def _run_build(root: str) -> None:
    """Run the build for `root`. Thread-unsafe by design; callers that run
    it on a fire-and-forget thread must wrap it in `_safe_run_build`."""
    build_id = str(uuid.uuid4())
    conn = init_db(root)
    try:
        on_disk = on_disk_files(root)
        added, removed = _diff_and_sync(conn, root, on_disk, build_id)
        # Remove stale entries
        if removed:
            remove_paths(conn, removed)
        # Parse new/changed files in batches
        batch: list = []
        for fpath in sorted(added):
            rows = _parse_safe(fpath)
            lang = _guess_lang(fpath)
            mtime, size = file_fingerprint(
                __import__("pathlib").Path(fpath))
            batch.append((fpath, mtime, size, lang, rows))
            if len(batch) >= BUILD_COMMIT_BATCH:
                _commit_batch(conn, root, build_id, batch)
                batch = []
        if batch:
            _commit_batch(conn, root, build_id, batch)
        # Invalidate cache so the next query re-walks (contract ②)
        invalidate_cache(root)
    finally:
        conn.close()


def _diff_and_sync(conn, root: str, on_disk, build_id: str):
    """Compute added/removed sets. Returns (added, removed)."""
    from .fingerprint import diff_indexed
    added, removed = diff_indexed(root, on_disk)
    return added, removed


def _parse_safe(path: str) -> list:
    """Parse a file, returning [] on any failure (fail-soft, gate ⑨)."""
    try:
        return parse_file(path)
    except Exception:
        return []


def _guess_lang(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    from .registry import EXT_TO_LANG
    return EXT_TO_LANG.get(ext, "unknown")


def _commit_batch(conn, root: str, build_id: str,
                  batch: list) -> None:
    """Upsert files + insert symbols, one commit per batch."""
    for fpath, mtime, size, lang, rows in batch:
        # F7 (restart-acceptance 2026-11-17): the L2.3c stamp must ride on
        # EVERY files-upsert path — this one (the host refresh / L3 build
        # commit batches) slipped through 8f2a4b9 with the default '',
        # re-manufacturing parser_gen_stale rows on every host-side
        # refresh of changed files.
        upsert_file(conn, fpath, mtime, size, lang, build_id, commit=False,
                    parser_gen=PARSER_GENERATION)
        insert_symbols(conn, root, fpath, rows, commit=False)
    conn.commit()
