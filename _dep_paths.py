"""Pure venv site-packages resolution for code_map dep loading.

Kept as a separate module so the resolver is unit-testable without triggering
__init__.py's import-time side effects. The dep-splice in __init__.py calls
:func:`resolve_dep_sites` and appends the result to ``sys.path``.

Resolution rules (venv 口径, user directive 10-08):
  1. Honour ``CODE_MAP_PY_SITE`` when set: use it verbatim (a single dir or a
     colon-separated list) if it exists.
  2. Otherwise glob ``<venv>/lib/python*/site-packages`` for EVERY Python minor
     that actually exists — never hardcode a minor version, so a venv built
     under 3.13, 3.14, or a future 3.15 all resolve.
  3. Fail-soft: a missing venv yields an empty list, never a crash.
"""
from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import List

ENV_OVERRIDE = "CODE_MAP_PY_SITE"


def resolve_dep_sites(venv_root: str) -> List[str]:
    """Return the existing site-packages dir(s) under ``venv_root``.

    The environment override wins when present; otherwise two glob patterns
    are tried in order:

      1. ``<root>/lib/python*/site-packages``  — standard venv layout
      2. ``<root>/*/lib/python*/site-packages`` — tools dir layout
         (e.g. ``~/.hermes/tools/python-3.14.7*/lib/python3.14/site-packages``)

    Results are sorted for deterministic sys.path ordering. Non-existent
    dirs are dropped.
    """
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        for candidate in override.split(os.pathsep):
            candidate = candidate.strip()
            if candidate and os.path.isdir(candidate):
                return [candidate]
        # override set but none of its dirs exist -> fall through to glob so a
        # stale env value can't silently orphan the dep lookup.
    patterns = [
        os.path.join(venv_root, "lib", "python*", "site-packages"),
        os.path.join(venv_root, "Lib", "site-packages"),
        os.path.join(venv_root, "*", "lib", "python*", "site-packages"),
    ]
    found: set = set()
    for pattern in patterns:
        found.update(glob.glob(pattern))
    return sorted(d for d in found if os.path.isdir(d))


def ensure_dep_paths() -> List[str]:
    """Splice :func:`resolve_dep_sites` results onto ``sys.path``.

    Intended to run before the package imports parser.py. Always appends the
    plugin-venv site-packages (deduped against existing ``sys.path`` entries —
    see the ``site not in sys.path`` guard below); it does NOT probe whether the
    host interpreter already sees the packages (the old find_spec early-return
    is gone, per the 10-08 venv 口径). Returns the dirs actually appended
    (for logging/tests). Rollback-safe: remove the call site to restore
    host-only behaviour.
    """
    import sys

    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    venv_root = os.environ.get("CODE_MAP_VENV", os.path.join(hermes_home, "plugin-venv"))
    sites = resolve_dep_sites(venv_root)
    appended: List[str] = []
    for site in sites:
        if site not in sys.path:
            sys.path.append(site)
            appended.append(site)
    return appended
