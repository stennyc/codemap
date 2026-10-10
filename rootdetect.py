"""rootdetect.py — project-root detection (contract ③, ⑤).

Searches upward from a file path for the nearest marker-bearing ancestor,
capped at $HOME. Contract ⑤: stat-only walk <2s budget; warn above 20k files.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from .config import ROOT_MARKERS, WALK_ESTIMATE_BUDGET_S, WALK_WARN_FILE_COUNT


def find_root_candidates(file_path: str) -> List[str]:
    """Return up to 4 candidate roots (nearest marker-bearing ancestors,
    capped at $HOME) for `file_path`, nearest first.
    """
    home = os.path.expanduser("~")
    cur = os.path.abspath(file_path)
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)

    candidates: List[str] = []
    while cur and cur != home:
        for marker in ROOT_MARKERS:
            if os.path.exists(os.path.join(cur, marker)):
                candidates.append(cur)
                break
        # Go up
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    # Cap at $HOME itself as a last resort
    if home not in candidates:
        candidates.append(home)
    return candidates[:4]


def detect_root(file_path: str) -> Optional[str]:
    """Return the nearest marker-bearing ancestor of `file_path` (or None)."""
    cands = find_root_candidates(file_path)
    return cands[0] if cands else None


def estimate_file_count(root: str) -> int:
    """Stat-only walk estimate (no content read) of indexable files under root.

    Contract ⑤: must complete <2s; above 20k files the caller should warn +
    invite narrowing.
    """
    import time
    import config as _cfg
    t0 = time.time()
    count = 0
    for _dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(tuple(_cfg.INDEXED_EXTENSIONS)):
                count += 1
                if time.time() - t0 > WALK_ESTIMATE_BUDGET_S:
                    return count
    return count
