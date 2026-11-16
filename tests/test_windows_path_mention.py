"""Windows drive-letter path-mention extraction (owner 2026-11-17, gotcha (b)).

The 2026-11-16 cross-platform fix made _extract_path_mentions recognize
Windows drive-letter paths, BUT the re-prepend dropped the DRIVE COLON:
line 345 built the mention as ``f"{drive}/{rest}"`` where the regex's group
1 is the drive LETTER only ("C"), so the result was "C/Users/..." instead of
"C:/Users/...". On Windows, ``os.path.abspath("C/Users/...")`` treats the
colon-less mention as a RELATIVE path and re-roots it onto the CWD, so a
clarify question that names a C:/.../supercompress target mints evidence for
the WRONG target (the mangled, CWD-re-rooted form) and the fail-closed
code_map_confirm_root gate stays blocked.

Fix: re-prepend the drive letter AND the colon (``f"{drive}:{rest}"``) so the
mention stays absolute under ``os.path.abspath`` on the host OS.

Case map (OS-independent on the mention STRING; the abspath assertion is
Windows-only):
  1. a backslash Windows question mention normalizes to the WITH-colon,
     forward-slash form ``C:/...`` (the mention keeps its drive colon) and the
     colon-less variant does NOT sneak in;
  2. backslash + forward-slash spellings DEDUP to the same single mention;
  3. [Windows-only] the mention's ``os.path.abspath`` preserves the drive
     (no CWD re-rooting) — the actual gotcha being fixed.

Run: a python that can ``import pytest`` + the plugin parent on sys.path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import sys as _sys
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent  # .../plugins
if str(_PLUGIN_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_PLUGIN_ROOT))
import pytest  # noqa: E402

from code_map import config as _config  # noqa: E402
from code_map.query import _extract_path_mentions  # noqa: E402

IS_WINDOWS = _config.IS_WINDOWS

# The canonical form the fix must produce: drive colon + forward slashes.
_CANON = "C:/Users/user/AppData/Local/hermes/plugins/supercompress"
# The buggy colon-less form that must NOT appear.
_BUGGY = "C/Users/user/AppData/Local/hermes/plugins/supercompress"
_Q_BACKSLASH = "index C:\\Users\\user\\AppData\\Local\\hermes\\plugins\\supercompress now?"
_Q_FWD = "index C:/Users/user/AppData/Local/hermes/plugins/supercompress now?"


def test_backslash_mention_keeps_drive_colon():
    m = _extract_path_mentions(_Q_BACKSLASH)
    assert _CANON in m, f"with-colon mention missing; got {m[:4]}"
    assert _BUGGY not in m, f"colon-less (re-rootable) mention still present: {m[:4]}"


def test_both_spellings_dedup_to_one_mention():
    mb = _extract_path_mentions(_Q_BACKSLASH)
    mf = _extract_path_mentions(_Q_FWD)
    # the canonical with-colon form must be the shared one
    assert _CANON in mb and _CANON in mf
    # and the colon-less form is absent from both
    assert _BUGGY not in mb and _BUGGY not in mf
    # backslash + forward-slash spellings of the same target collapse to one
    common = [x for x in mb if x.startswith("C:")]
    assert [x for x in mf if x.startswith("C:")] == common


@pytest.mark.skipif(not IS_WINDOWS, reason="CWD re-rooting is a Windows abspath behavior")
def test_windows_mention_abspath_preserves_drive():
    # the actual gotcha: os.path.abspath of the extracted mention must keep the
    # drive letter, not re-root onto the CWD.
    m = [x for x in _extract_path_mentions(_Q_BACKSLASH) if x.startswith("C:")]
    assert m, "a C: (with-colon) Windows mention must be extracted"
    ap = os.path.abspath(m[0])
    assert ap == "C:\\Users\\user\\AppData\\Local\\hermes\\plugins\\supercompress", (
        f"abspath re-rooted the drive-less mention onto the CWD: got {ap}")
