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
import json
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


def _find_hermes_managed_venv(hermes_home: str) -> str | None:
    """Return the venv path stored in Hermes' ``facts.json``.

    Hermes' package manager records the active venv in
    ``$HERMES_HOME/installs/<install_key>/facts.json`` under
    ``packages.venv.environment``.  This is the canonical source of truth —
    the path changes with each Hermes update and must never be hardcoded.

    Returns ``None`` when no facts file is found (fresh install, offline mode).
    """
    installs_dir = os.path.join(hermes_home, "installs")
    if not os.path.isdir(installs_dir):
        return None
    for key_dir in os.listdir(installs_dir):
        facts_path = os.path.join(installs_dir, key_dir, "facts.json")
        if not os.path.isfile(facts_path):
            continue
        try:
            with open(facts_path, encoding="utf-8-sig") as f:
                facts = json.load(f)
            venv = facts.get("packages", {}).get("venv", {}).get("environment")
            if venv and os.path.isdir(venv):
                return venv
        except (json.JSONDecodeError, OSError, KeyError):
            continue
    return None


def ensure_dep_paths() -> List[str]:
    """Splice resolved site-packages onto ``sys.path``.

    Priority order:
      1. ``CODE_MAP_PY_SITE`` environment override (explicit user control)
      2. Hermes-managed venv (from ``facts.json`` — canonical, auto-detected)
      3. Nothing — fail-soft, deps resolved from host interpreter if present

    Deduplication against ``sys.path`` is applied so repeated calls are no-ops.
    Returns the dirs actually appended (for logging/tests).
    """
    import sys

    # F5 ruling (owner 2026-11-17, option C — "record the intentional
    # divergence, do NOT change behaviour"): this resolution is DELIBERATELY
    # different from config.hermes_home().
    #   - config.hermes_home() returns the PROFILE home (where the profile's
    #     data dir lives, e.g. .../hermes/profiles/<name> on this host).
    #   - _dep_paths needs the INSTALL home — the dir that holds `installs/`
    #     (the shared managed-venv tree; .../hermes/installs on this host,
    #     %LOCALAPPDATA%\hermes on Windows) so _find_hermes_managed_venv can
    #     resolve the canonical venv. Pointing it at the profile home would
    #     break the lookup (there is no installs/ under a profile home).
    # So keep HERMES_HOME-env > ~/.hermes here; do NOT "dedup" to
    # config.hermes_home() — that is a different layer (owner ruling).
    hermes_home = os.environ.get(
        "HERMES_HOME", str(Path.home() / ".hermes")
    )

    candidates: List[str] = []

    # 1. Explicit override
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        for candidate in override.split(os.pathsep):
            candidate = candidate.strip()
            if candidate and os.path.isdir(candidate):
                candidates.append(candidate)

    # 2. Hermes-managed venv (canonical)
    if not candidates:
        managed = _find_hermes_managed_venv(hermes_home)
        if managed:
            candidates.extend(resolve_dep_sites(managed))

    # Splice onto sys.path (deduped)
    appended: List[str] = []
    for site in candidates:
        if site not in sys.path:
            sys.path.append(site)
            appended.append(site)
    return appended
