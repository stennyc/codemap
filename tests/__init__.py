"""Tests package for code_map.

Location-independent sys.path bootstrap: make `code_map` importable no
matter where this checkout physically lives (the real profile path
/root/.hermes/plugins/code_map, a git worktree under a scratch dir, a
clone anywhere else).

The naive `Path(__file__).resolve().parent.parent.parent` bootstrap works
only when the repo physically sits at <something>/code_map — i.e. when
the test file's grand-parent IS the dir containing the `code_map` package
(reviewer defect 2026-10-11: worktree checkouts broke with
ModuleNotFoundError: No module named 'code_map').

So instead we WALK UPWARD from this file until we find a directory that
CONTAINS a `code_map` subdirectory whose `__init__.py` is this very
package's ancestor chain — that directory is the plugin root, and it is
the single, physical root we put on sys.path (prevents the double-module
split that caused the 0-symbol tree-sitter incident).
"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent   # .../code_map (tests' parent)


def _find_plugin_root(start: Path) -> Path:
    """Walk up from `start` to the dir that physically contains the
    `code_map` package whose tests/ dir is an ancestor of `start`.

    Returns the plugin root (the dir containing `code_map/`). Fails soft:
    if no such dir exists (e.g. the package was copied out of its tree)
    fall back to the 3-levels-up assumption so at least the real-profile
    layout still works.
    """
    cur = _PKG_DIR  # .../code_map
    while cur.parent != cur:
        cand_pkg = cur.parent / "code_map"
        if (cand_pkg / "__init__.py").is_file() and _is_same_pkg(cand_pkg):
            return cur.parent
        cur = cur.parent
    # Fallback: classic <plugin_root>/code_map layout.
    return _PKG_DIR.parent


def _is_same_pkg(candidate_pkg: Path) -> bool:
    """True iff `candidate_pkg/tests` resolves to THIS file's tests dir
    (i.e. the package we are part of, not a sibling copy on disk)."""
    cand_tests = candidate_pkg / "tests"
    if not cand_tests.is_dir():
        return False
    try:
        return cand_tests.resolve() == _PKG_DIR.resolve() / "tests"
    except OSError:
        return False


_PLUGIN_ROOT = _find_plugin_root(Path(__file__).resolve().parent)


def _ensure_plugin_root_on_path() -> None:
    # Already importable: keep the existing resolution so we never create
    # a second `code_map` module object (single-root rule).
    if "code_map" in sys.modules:
        return
    for p in sys.path:
        if p and Path(p).resolve() == _PLUGIN_ROOT.resolve():
            return
    sys.path.insert(0, str(_PLUGIN_ROOT))


_ensure_plugin_root_on_path()
