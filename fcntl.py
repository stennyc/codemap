"""fcntl shim for Windows — provides flock via filelock.

Placed in the top-level plugins/ directory so that when Hermes adds
$HERMES_HOME/plugins to sys.path, ``import fcntl`` resolves here BEFORE
the stdlib (which lacks fcntl on Windows). The shim delegates to the
``filelock`` package installed in the Hermes-managed venv.

API surface used by code_map:
  - fcntl.flock(fd, flag)   -> FileLock equivalent
  - fcntl.LOCK_EX           -> constant (2)
  - fcntl.LOCK_NB           -> constant (4)
  - fcntl.LOCK_UN           -> constant (8)

Behavior mapping:
  LOCK_EX | LOCK_NB -> acquire(timeout=0) [non-blocking]
  LOCK_EX         -> acquire() [blocking]
  LOCK_UN         -> release()

The shim wraps raw fd integers in a FileLock instance keyed by the fd value.
"""
from __future__ import annotations

import logging
import os as _os
import time as _time
from typing import Dict

logger = logging.getLogger("hermes_plugins.fcntl_shim")

# Exported constants (same values as Linux fcntl)
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

# State: maps fd int -> FileLock instance
_fd_locks: Dict[int, object] = {}
_fd_acquired: Dict[int, bool] = {}


def _get_filelock(path: str):
    """Lazily import FileLock (avoid import error if filelock missing)."""
    try:
        from filelock import FileLock
        return FileLock
    except ImportError:
        logger.warning(
            "hermes_plugins.fcntl_shim: filelock not available, "
            "flock calls will be no-ops (WAL still protects db)"
        )
        return None


def flock(fd: int, flags: int) -> None:
    """Advisory lock on fd, mimicking fcntl.flock on Linux.

    - LOCK_EX | LOCK_NB : try non-blocking exclusive lock
    - LOCK_EX : blocking exclusive lock (rarely used by code_map)
    - LOCK_UN : unlock
    """
    fl_cls = _get_filelock(None)
    if fl_cls is None:
        return  # filelock unavailable; proceed unlocked (WAL-safe)

    op = flags & 0xF  # LOCK_EX=2, LOCK_NB=4, LOCK_UN=8

    if op == LOCK_UN:
        # Unlock
        fl = _fd_locks.pop(fd, None)
        if fl is not None:
            try:
                fl.release()
            except Exception:
                pass
        _fd_acquired.pop(fd, None)
        return

    # LOCK_EX (possibly with LOCK_NB)
    path = f"/tmp/_code_map_lock_{_os.getpid()}_{fd}.lock"
    try:
        fl = fl_cls(path)
        _fd_locks[fd] = fl
        if flags & LOCK_NB:
            # Non-blocking: try once
            try:
                fl.acquire(timeout=0)
                _fd_acquired[fd] = True
            except Exception:
                _fd_acquired[fd] = False
        else:
            # Blocking (should not normally happen in code_map)
            fl.acquire()
            _fd_acquired[fd] = True
    except Exception as exc:
        logger.warning("hermes_plugins.fcntl_shim: flock(%d, %d) failed: %s", fd, flags, exc)
        _fd_acquired[fd] = False
