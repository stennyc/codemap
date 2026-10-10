"""prune.py — directory pruning predicates (contract ③, ⑤).

Business dirs that are neither blacklisted nor gitignored must NEVER be
pruned. .gitignore has HIGHEST priority over the blacklist.
"""
from __future__ import annotations

import os
from typing import Set

from .config import DEFAULT_PRUNE_BLACKLIST


def should_prune_dir(dir_name: str, gitignored: bool = False) -> bool:
    """Return True if `dir_name` should be pruned from the walk.

    .gitignored=True overrides everything (contract ③: gitignore is
    HIGHEST priority). Blacklist entries are the default pruning set.
    """
    if gitignored:
        return True
    return dir_name in DEFAULT_PRUNE_BLACKLIST


def is_indexable(filename: str) -> bool:
    """Return True if `filename` has an indexed extension."""
    from .config import INDEXED_EXTENSIONS
    return any(
        filename.endswith(ext) for ext in INDEXED_EXTENSIONS
    )
