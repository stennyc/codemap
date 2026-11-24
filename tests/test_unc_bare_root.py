"""§2.22 (2026-11-24, aligns Go osenv.go:183-189): the Windows
branch of is_bare_fs_root must reject the three UNC bare-root
forms (mirrors Go IsBareFSRoot's `BareUNCRe` arm):

  \\\\            (the bare current-drive marker)
  \\\\server      (a whole-server, no share)
  \\\\server\\share  (a whole-share root)

A UNC path with a SUB-share (\\\\server\\share\\sub) is NOT bare —
it names a real sub-tree and stays indexable. POSIX is unchanged
("" or "/" are bare; UNC literals re-root onto the CWD drive on
Windows and are ordinary relative dir names on POSIX).

Host-agnostic: the predicate is a pure string check (the Go port
makes the same contract), so it is testable on any host OS.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # 自适应 bootstrap: plugin 根

from code_map import config

# The Go BareUNCRe's three arms, spelled with literal backslashes
# (the Windows separator); both / and \ are accepted as separators.
BARE_UNC_CASES = (
    "\\\\server",             # server-only (no share)
    "\\\\server\\",           # server-only with trailing sep
    "\\\\server/share",       # whole-share, forward slash
    "\\\\server\\share",      # whole-share, backslash
    "\\\\server\\share\\",    # whole-share with trailing sep
)

# NOT bare: a sub-tree under the share.
NON_BARE_UNC_CASES = (
    "\\\\server\\share\\sub",
    "\\\\server/share/sub",
    "\\\\server\\share\\deep\\path",
)


def test_unc_bare_roots_rejected_windows():
    for p in BARE_UNC_CASES:
        assert config.is_bare_fs_root(p, True) is True, \
            f"bare UNC root {p!r} must be rejected on Windows (Go §2.22 align)"


def test_unc_subtree_not_bare_windows():
    for p in NON_BARE_UNC_CASES:
        assert config.is_bare_fs_root(p, True) is False, \
            f"a UNC sub-tree {p!r} is NOT bare (a real indexable sub-dir)"


def test_unc_posix_unchanged():
    # On POSIX the UNC literals re-root / are ordinary relative names —
    # is_bare_fs_root(POSIX) only sees "" and "/".
    for p in BARE_UNC_CASES + NON_BARE_UNC_CASES:
        assert config.is_bare_fs_root(p, False) is False, \
            f"POSIX: {p!r} is not a bare fs root"
    assert config.is_bare_fs_root("", False) is True
    assert config.is_bare_fs_root("/", False) is True


def test_bare_drive_still_rejected_windows():
    # The existing drive-root arm must keep working alongside UNC.
    for p in ("C:\\", "C:/", "C:", "c:"):
        assert config.is_bare_fs_root(p, True) is True, \
            f"bare drive root {p!r} must stay rejected"
