"""Tests package for code_map.

Auto sys.path bootstrap: make `code_map` importable regardless of where
this repo physically lives. Each test file inserts the plugin root
(this file's `parent.parent.parent`, i.e. the dir that physically
contains `code_map/`) into sys.path inline before importing anything
from code_map — see the `sys.path.insert` line in every test_*.py.

This package's `__init__` additionally runs when any test does
`from code_map.tests import ...` (e.g. `fixtures`); it enforces the
single-root rule: if the plugin root is already on sys.path it does
nothing, otherwise it inserts it — preventing a second `code_map`
module object (the tree-sitter grammar-cache split that caused the
0-symbol test incident).

Known layout boundary (reviewer defect 2026-10-11 #3):
  - `parent.parent.parent` works when the repo physically sits at
    <plugin_root>/code_map (the real profile path: /root/.hermes/plugins).
  - It does NOT work in a bare `git worktree add <scratch>/cm_vt`
    checkout, where the repo root IS the code_map package dir and there
    is no enclosing <plugin_root> level.  In that case `import code_map`
    cannot resolve at all (the directory is not named `code_map`), and
    the test suite cannot run without first creating a symlink
    `<scratch>/code_map -> cm_vt` or installing with `pip install -e`.
    This is a directory-naming constraint, not a code bug.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent          # .../code_map
_PLUGIN_ROOT = _PKG_DIR.parent                              # .../plugins


def _ensure_plugin_root_on_path() -> None:
    # Already importable (e.g. pytest run from the plugin root, or the
    # interpreter was started there): keep the existing resolution so we
    # never create a second `code_map` module object.
    if "code_map" in sys.modules:
        return
    # A test file inserted the plugin root explicitly: keep it if it is
    # the correct physical root (avoids re-registering a stale copy).
    for p in sys.path:
        if p and Path(p).resolve() == _PLUGIN_ROOT.resolve():
            return
    sys.path.insert(0, str(_PLUGIN_ROOT))


_ensure_plugin_root_on_path()
