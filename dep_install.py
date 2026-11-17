"""dep_install — auto-install of missing tree-sitter grammar packages.

Owner directive (2026-11-17): the code_map dep venv self-heals its
grammar set. The COMPLETE required list is registry.SUPPORTED_LANGUAGES
(11 grammars; the pip names are pinned in requirements.txt — a test
keeps the two in lock-step). The managed venv is resolved via
``_dep_paths._find_hermes_managed_venv`` (facts.json — the canonical
source, never hardcoded); a pip-less venv is handled by preferring
``uv`` when it is on PATH.

Wired into the plugin bootstrap (__init__._maybe_auto_install_grammars,
fail-soft): on gateway start and on every build-subprocess entry (a
fresh interpreter re-splices + re-probes), a missing-grammar set
triggers a rate-limited install attempt. Newly installed grammars are
visible to FRESH processes immediately; the importing process sees
them at its next restart (its module imports already happened).

Env controls:
  CODE_MAP_AUTO_INSTALL       "on" (default) | "off" | "dry"
  CODE_MAP_AUTO_INSTALL_VENV  explicit venv root override (rare)

The pure core (pip-name mapping, missing-diff, argv build, marker
rate-limit, target resolution) is unit-testable without a network;
the I/O wrapper takes an injected runner so tests never touch the
real venvs. Fail-soft posture: any exception becomes a report dict,
never a raise — a dep self-heal problem must not break the plugin.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Mapping, Optional, Tuple

from . import registry  # noqa: F401  (pure module; re-exported for tests' monkeypatch)

AUTO_INSTALL_ENV = "CODE_MAP_AUTO_INSTALL"          # on (default) | off | dry
TARGET_VENV_ENV = "CODE_MAP_AUTO_INSTALL_VENV"      # explicit venv root override
MARKER_NAME = "_dep_autoinstall.json"
MARKER_MAX_AGE_H = 24.0
INSTALL_TIMEOUT_S = 600


# ── pure core ─────────────────────────────────────────────────────────────────

def pip_name_for(module: str) -> str:
    """Grammar module name -> pip package name (tree_sitter_java -> tree-sitter-java).

    Non-grammar module names pass through (underscores -> dashes), so the
    helper stays total."""
    if module.startswith("tree_sitter_"):
        return "tree-sitter-" + module.split("tree_sitter_", 1)[1]
    return module.replace("_", "-")


def grammar_packages() -> Dict[str, str]:
    """The complete grammar set code_map needs: lang -> pip package.

    registry.SUPPORTED_LANGUAGES is the single source of truth (it
    drives both the parser's import probes and the requirements.txt
    lock-step test)."""
    from . import registry
    return {lang: pip_name_for(mod)
            for lang, mod in registry.SUPPORTED_LANGUAGES.items()}


def missing_packages(available: List[str],
                     packages: Optional[Dict[str, str]] = None) -> List[str]:
    """Pip names of the grammars NOT in ``available`` (sorted)."""
    packages = packages if packages is not None else grammar_packages()
    have = set(available)
    return sorted(pkgs for lang, pkgs in packages.items() if lang not in have)


def build_install_argv(target_python: str, packages: List[str],
                       use_uv: bool) -> List[str]:
    """Exact argv for the install attempt.

    uv form:  uv pip install --python <target> <pkgs...>   (handles
    pip-less venvs, e.g. the managed toolchain venv)
    pip form: <target> -m pip install <pkgs...>"""
    if use_uv:
        return ["uv", "pip", "install", "--python", target_python, *packages]
    return [target_python, "-m", "pip", "install", *packages]


def resolve_managed_venv_python() -> Optional[str]:
    """The Hermes-managed venv's python interpreter, or None.

    facts.json (via _find_hermes_managed_venv) is the canonical venv
    source; the platform layout (Scripts/python.exe vs bin/python) is
    derived, never hardcoded per-host."""
    from ._dep_paths import _find_hermes_managed_venv, _install_home_fallback
    hermes_home = os.environ.get("HERMES_HOME") or _install_home_fallback()
    venv = _find_hermes_managed_venv(hermes_home)
    if not venv:
        return None
    if os.name == "nt":
        cand = os.path.join(venv, "Scripts", "python.exe")
    else:
        cand = os.path.join(venv, "bin", "python")
    return cand if os.path.isfile(cand) else None


def marker_due(marker: Optional[dict], now: float, missing: List[str]) -> bool:
    """Rate-limit predicate: due when no marker, marker >24h old, or the
    missing set CHANGED since the last attempt (a new gap must not wait
    out a fresh marker)."""
    if marker is None:
        return True
    if now - float(marker.get("ts", 0.0)) > MARKER_MAX_AGE_H * 3600:
        return True
    return sorted(marker.get("missing", [])) != sorted(missing)


# ── I/O wrapper (fail-soft; runner is injectable for tests) ─────────────────

def _marker_path() -> Optional[str]:
    try:
        from .config import data_dir
        d = data_dir()
        return os.path.join(d, MARKER_NAME) if d else None
    except Exception:
        return None


def _run_default(argv: List[str], timeout: int) -> Tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout)
        tail = ((p.stdout or "")[-400:] + (p.stderr or "")[-400:]).strip()
        return p.returncode, tail
    except Exception as e:  # spawn/timeout/hiccup -> fail-soft
        return 1, str(e)


def maybe_auto_install(runner=None, now: Optional[float] = None,
                       env: Optional[Mapping[str, str]] = None) -> dict:
    """One fail-soft auto-install attempt. Returns a report dict:

      {mode, attempted, exit_code, missing, target, argv,
       skipped_reason}

    Never raises (a dep self-heal problem must not break the plugin
    bootstrap)."""
    now = time.time() if now is None else now
    env = os.environ if env is None else env
    mode = str(env.get(AUTO_INSTALL_ENV, "on")).strip().lower()
    rep: dict = {"mode": mode, "attempted": False, "exit_code": None,
                 "missing": [], "target": None, "argv": None,
                 "skipped_reason": None, "error_tail": None}
    if mode == "off":
        rep["skipped_reason"] = f"{AUTO_INSTALL_ENV}=off"
        return rep
    try:
        rep["missing"] = missing_packages(registry.available_languages())
        if not rep["missing"]:
            rep["skipped_reason"] = "none missing"
            return rep

        venv_root = str(env.get(TARGET_VENV_ENV, "")).strip()
        if venv_root:
            if os.name == "nt":
                target = os.path.join(venv_root, "Scripts", "python.exe")
            else:
                target = os.path.join(venv_root, "bin", "python")
            target = target if os.path.isfile(target) else sys.executable
        else:
            target = resolve_managed_venv_python() or sys.executable
        rep["target"] = target

        # uv first (handles pip-less venvs); fall back to the target's pip.
        use_uv = shutil.which("uv") is not None
        rep["argv"] = build_install_argv(target, rep["missing"], use_uv)
        if mode == "dry":
            rep["skipped_reason"] = "dry-run"
            return rep
        if not use_uv and not (
                os.path.isfile(target) and _target_has_pip(target)):
            rep["skipped_reason"] = ("no usable installer: uv absent and "
                                     "target venv has no pip")
            return rep

        mp = _marker_path()
        marker = None
        if mp and os.path.isfile(mp):
            try:
                with open(mp, encoding="utf-8") as f:
                    marker = json.load(f)
            except Exception:
                marker = None
        if not marker_due(marker, now, rep["missing"]):
            rep["skipped_reason"] = "marker fresh + same missing set"
            return rep

        _runner = runner if runner is not None else _run_default
        try:
            exit_code, tail = _runner(rep["argv"], INSTALL_TIMEOUT_S)
        except Exception as e:  # an exploded runner is fail-soft: report, not raise
            exit_code, tail = 1, str(e)
        rep["attempted"] = True
        rep["exit_code"] = exit_code
        rep["error_tail"] = tail
        if mp:
            os.makedirs(os.path.dirname(mp), exist_ok=True)
            rec = {"ts": now, "missing": rep["missing"], "target": target,
                   "exit_code": exit_code}
            tmp = mp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f)
            os.replace(tmp, mp)
    except Exception as e:  # fail-soft: report, never raise
        rep["skipped_reason"] = f"fail-soft: {e}"
    return rep


def _target_has_pip(target_python: str) -> bool:
    """Does the target interpreter have a usable pip module? (one fast
    probe subprocess; cached per-path per process is overkill here — the
    caller already rate-limits via the marker.)"""
    try:
        p = subprocess.run(
            [target_python, "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=30)
        return p.returncode == 0
    except Exception:
        return False
