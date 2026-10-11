"""Tests package for code_map.

Auto sys.path bootstrap: make `code_map` importable regardless of where
this repo physically lives (the real profile path, a git worktree under a
scratch dir, or any other checkout location). Search upward from this
file for a `code_map` package dir and register its PARENT (the plugin
root, i.e. the dir that contains `code_map/`) on sys.path exactly once.

After importing this package, `import code_map` and
`from code_map.tests import fixtures` resolve to the SAME physical
package — the double-module split (tree-sitter grammar cache split) that
caused the 0-symbol test incident is prevented by this single-root rule.
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
