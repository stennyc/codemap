"""lockfile.py — cross-process write mutex for code_map's shared index.

SQLite WAL + busy_timeout already keeps the db CORRUPT-FREE under
concurrent writers (multi-process, single-physical-file via hardlink).
What WAL does NOT give you is *serialization* of whole operations:

  - two host processes could both run a full _run_build over the same
    target at once (two 30s-ish walks stacking, one losing the other's
    diff window);
  - a write-side single-file upsert could interleave with a full build's
    diff snapshot (build's `diff_indexed` sees a half-upserted state).

fcntl.flock on a shared lock file serializes those. The lock file lives
in the SAME physical dir as the dbs (data_dir(), which is hardlinked
across profile paths), so two profiles' processes contend on one lock —
exactly the guarantee we want. Fail-soft: if flock is unavailable or
the lock file can't be opened, callers proceed WITHOUT the lock (WAL
still protects the db; we only lose the serialization nicety).
"""
from __future__ import annotations

import fcntl
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .config import data_dir

_log = logging.getLogger("code_map.lockfile")

# One lock per physical index store: all per-root dbs share a data dir,
# and the diff/remove semantics only ever touch one root at a time, so a
# single global lock is correct and simpler than per-root files.
_LOCK_NAME = "_proc.lock"
_HEAL_ACQUIRE_TIMEOUT_S = 25.0  # < host's 30s bounded-hook budget (reviewer F1)


def _lock_path() -> Path:
    return data_dir() / _LOCK_NAME


@contextmanager
def write_lock(nowait: bool = False, timeout_s: Optional[float] = None,
               what: str = "write") -> Iterator[None]:
    """Advisory cross-process write lock for the `with` body.

    The body ALWAYS runs (flock is advisory, not a gate): SQLite WAL +
    busy_timeout already keep the db correct under concurrent writers —
    this lock only adds operation-level *serialization* when it can be
    obtained cheaply.

    nowait=True  -> try once with LOCK_NB; whether or not the lock is
        acquired the body runs (advisory). Used by the ms-scale
        single-file reindex, which must never block a host worker.

    nowait=False uses LOCK_NB polling against a deadline (never an
    unbounded blocking flock): a full-build heal must stay inside the
    host's 30s bounded-hook budget, so if the lock is held past the
    deadline we proceed WITHOUT it (WAL still keeps the db safe) rather
    than wedge the worker.

    Fail-soft: any OSError / missing fcntl / timeout -> proceed without
    the lock; a warning is logged so a misconfigured deploy is visible,
    not silent.
    """
    path = _lock_path()
    handle: Optional[int] = None
    acquired = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        handle = fd
        deadline = (_now() + (timeout_s if timeout_s is not None
                              else _HEAL_ACQUIRE_TIMEOUT_S))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if nowait:
                    break  # advisory: body runs unlocked
                if _now() >= deadline:
                    break  # past the host budget; proceed unlocked (WAL-safe)
                time.sleep(0.05)
    except (OSError, ImportError) as exc:
        _log.warning("code_map cross-process write lock unavailable (%s); "
                     "proceeding without it — WAL still protects the db", exc)
        handle = None
    try:
        yield
    finally:
        if handle is not None and acquired:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass


def _now() -> float:
    import time
    return time.monotonic()
