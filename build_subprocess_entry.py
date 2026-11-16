#!/usr/bin/env python3
"""code_map build-subprocess entry (the process-isolated full-build runner).

Invoked by state._start_build_process as:

    <sys.executable> build_subprocess_entry.py <target>

It runs the heavy full index build (tree-sitter parse + sqlite commit) in a
CHILD process so it does NOT share the main process's GIL / CPU / memory
(owner 2026-11-17 — code indexing is pure mechanical, zero LLM, so unlike
filemem there is no in-process main_runtime / in-memory panel that keeps the
build pinned to the host process). It self-bootstraps exactly like the host
loader: `import code_map` installs the fcntl shim + splices tree-sitter onto
sys.path (ensure_dep_paths) + the relative-import guards, then
`state._run_build(target)` runs under the cross-process write_lock
(advisory; sqlite WAL + busy_timeout keep the db correct regardless of
whether the lock is held, so a lock-timeout never wedges a build).

Exit codes:
    0  = built (index committed; a no-op empty build also exits 0)
    1  = a build failure (the traceback is printed to stderr, so a broken
         build is VISIBLE — mirrors _safe_run_build's fail-visible posture,
         never a silent no-op)
    2  = bad argv (no target)
"""
import os
import sys


def _main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("usage: build_subprocess_entry.py <target>", file=sys.stderr)
        return 2
    target = sys.argv[1].strip()

    # Bootstrap the code_map package the way the host loader does: put the
    # plugin dir's PARENT on sys.path so `import code_map` works, and let
    # __init__.py self-install the fcntl shim + splice tree-sitter
    # (ensure_dep_paths) before the relative imports run.
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(plugin_dir)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    import code_map  # noqa: F401  (fcntl shim + ensure_dep_paths + rel imports)
    from code_map import state
    from code_map.lockfile import write_lock

    # Cross-process build lock (advisory + fail-soft): serialize a full-walk
    # build against a sibling build / the hot heal path when it can be
    # obtained cheaply; a timeout proceeds WITHOUT the lock (sqlite WAL still
    # keeps the db correct) — never wedge a fire-and-forget build.
    with write_lock(nowait=False, what="build-subprocess"):
        state._run_build(target)  # raises on a broken build -> exit 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main())
    except Exception:
        import traceback
        traceback.print_exc()  # VISIBLE failure (mirrors _safe_run_build)
        sys.exit(1)
