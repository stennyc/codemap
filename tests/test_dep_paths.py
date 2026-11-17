"""F-B (owner 2026-11-17): the _dep_paths HERMES_HOME fallback is OS-canonical.

On Windows the fallback was the phantom home-dir ``~/.hermes`` (a second tree
that never holds installs/ on this host). F-B aligns it to the real install
home ``%LOCALAPPDATA%\\hermes``; POSIX keeps ``~/.hermes``. This is still
deliberately NOT config.hermes_home() (the profile home, a different layer).
"""

from __future__ import annotations

import sys
from pathlib import Path

import sys as _sys
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_PLUGIN_ROOT))

from code_map import _dep_paths  # noqa: E402


def test_install_home_fallback_windows_uses_localappdata(monkeypatch):
    """F-B: Windows fallback is %LOCALAPPDATA%\\hermes (deterministic fake LA)."""
    monkeypatch.setenv("LOCALAPPDATA", r"C:\fake\la")
    assert _dep_paths._install_home_for("nt") == r"C:\fake\la\hermes"


def test_install_home_fallback_windows_ignores_home_dot_hermes(monkeypatch):
    """F-B: the Windows fallback must NOT derive from the home dir (no phantom
    ~/.hermes tree); with a home set it still resolves under LOCALAPPDATA."""
    monkeypatch.setenv("LOCALAPPDATA", r"C:\fake\la")
    monkeypatch.setenv("USERPROFILE", r"C:\fake\home")
    assert _dep_paths._install_home_for("nt") == r"C:\fake\la\hermes"


def test_install_home_fallback_posix_uses_home_dot_hermes(monkeypatch):
    """F-B: the POSIX fallback stays ~/.hermes and ignores LOCALAPPDATA.
    (On this Windows test host Path.home() resolves to the real home; the
    assertion checks only the .hermes leaf + that LOCALAPPDATA is unused.)"""
    monkeypatch.setenv("LOCALAPPDATA", r"C:\fake\la")  # must be ignored on POSIX
    assert _dep_paths._install_home_for("posix").endswith(".hermes")
