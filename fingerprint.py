"""File fingerprinting + B+ scandir diff for code_map.

keyed by abspath(root) single key; epoch stored in cache value, re-fetched
from DB on each cold miss (contract ②: rescan must not be swallowed by cache).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, FrozenSet, Set, Tuple

from .config import INDEXED_EXTENSIONS

# LRU cache: keyed by abspath(root) single key; epoch stored in cache value,
# re-fetched from DB on each cold miss. TTL=60s.
_cache: Dict[str, Tuple[int, FrozenSet[str], float]] = {}  # root -> (epoch, paths, ts)
_CACHE_TTL_S = 60.0


def file_fingerprint(path: Path) -> Tuple[float, int]:
    """(mtime, size) fingerprint for a single file. Fail-soft on OSError."""
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, 0)


def invalidate_cache(root: str) -> None:
    """Drop the cache entry for `root`. Called by state.py after each build."""
    _cache.pop(os.path.abspath(root), None)


def on_disk_files(root: str) -> FrozenSet[str]:
    """Return the set of indexable file paths under `root` (B+ walk).

    Uses os.scandir recursion (O(dirs) syscalls, not O(files)) for the B+
    pass. Pruning is delegated to should_prune_dir / is_indexable so the
    cache layer never re-implements it.
    """
    root = os.path.abspath(root)
    # Cache hit: return frozen set directly (cheap, no B+ walk)
    entry = _cache.get(root)
    if entry is not None:
        _epoch, _paths, ts = entry
        if time.time() - ts < _CACHE_TTL_S:
            return _paths

    # Cache miss / cold path: full B+ scandir walk
    paths: Set[str] = set()
    _walk(root, paths)
    frozen = frozenset(paths)
    _cache[root] = (0, frozen, time.time())
    return frozen


def _walk(dirpath: str, out: Set[str]) -> None:
    """Recursive os.scandir walk. Fail-soft on OSError (skip dir)."""
    try:
        entries = list(os.scandir(dirpath))
    except OSError:
        return
    subdirs: list[str] = []
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                subdirs.append(entry.path)
            elif entry.is_file(follow_symlinks=False):
                name = os.path.basename(entry.path)
                if name.endswith(tuple(INDEXED_EXTENSIONS)):
                    out.add(entry.path)
        except OSError:
            continue
    for d in sorted(subdirs):
        _walk(d, out)


def diff_indexed(root: str, on_disk: FrozenSet[str]) -> Tuple[Set[str], Set[str]]:
    """Diff on-disk paths against the DB index. Returns (added, removed)."""
    from .db import init_db, stale_paths
    db = init_db(root)
    try:
        removed = stale_paths(db, root, on_disk)
        # added = on_disk - indexed
        indexed = set()
        for r in db.execute("SELECT path FROM files WHERE path LIKE ?",
                            (f"{root}/%",)):
            indexed.add(r[0])
        added = set(on_disk - indexed)
        return added, removed
    finally:
        db.close()
