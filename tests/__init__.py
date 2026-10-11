"""Tests package for code_map.

Location-independent sys.path bootstrap: make `code_map` importable no
matter where this checkout physically lives (the real profile path
/root/.hermes/plugins/code_map, a git worktree under a scratch dir, a
clone anywhere else).

The naive `Path(__file__).resolve().parent.parent.parent` bootstrap works
only when the repo physically sits at <plugin_root>/code_map AND the test
suite is run such that that is 3 levels up — a git worktree breaks it
(reviewer defect 2026-10-11 #3: 6 collection errors,
ModuleNotFoundError: No module named 'code_map'), because in a worktree
the plugin root IS the worktree root, not the test file's grandparent.

So we WALK: starting from this file's own directory, find the nearest
ancestor directory `D` such that `D/code_map/__init__.py` exists AND
`D/code_map/tests` resolves to this tests dir (i.e. the `code_map`
package physically owns this tests/ — not a sibling copy on disk). That
`D` is the plugin root; it is the single physical root we put on
sys.path (prevents the double-module / tree-sitter grammar-cache split
that caused the 0-symbol test incident).

This covers BOTH layouts with one rule:
  - real profile:   D = /root/.hermes/plugins            (contains code_map/)
  - git worktree:   D = <worktree_root>                   (contains code_map/)
  - deep scratch:   D = <the dir that actually holds code_map/>
"""
from __future__ import annotations

import sys
from pathlib import Path

_THIS_TESTS_DIR = Path(__file__).resolve().parent   # .../code_map/tests


def _is_same_pkg(candidate_pkg: Path) -> bool:
    """True iff `candidate_pkg/tests` resolves to THIS tests dir — i.e. the
    `code_map` package we are physically part of, not a sibling copy."""
    cand_tests = candidate_pkg / "tests"
    if not cand_tests.is_dir():
        return False
    try:
        return cand_tests.resolve() == _THIS_TESTS_DIR.resolve()
    except OSError:
        return False


def _find_plugin_root() -> Path:
    """Nearest ancestor dir D of this tests dir with a physical `code_map/`
    package whose tests/ is this one. Fail-soft: if none (package copied
    out of its tree), fall back to the tests dir's grandparent (the classic
    <plugin_root>/code_map layout) so the real-profile case still works.
    """
    cur = _THIS_TESTS_DIR
    while True:
        cand_pkg = cur / "code_map"
        if (cand_pkg / "__init__.py").is_file() and _is_same_pkg(cand_pkg):
            return cur
        if cur.parent == cur:
            break  # walked to filesystem root without a match
        cur = cur.parent
    return _THIS_TESTS_DIR.parent.parent  # fallback: .../plugins/code_map -> .../plugins


_PLUGIN_ROOT = _find_plugin_root()


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
