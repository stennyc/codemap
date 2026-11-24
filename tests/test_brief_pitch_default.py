"""§2.22 (2026-11-24, aligns Go config.go:110): BRIEF_BUILD_PITCH
default is ON — the DSH owner ruling (2026-11-21) turned the pitch
on by default; the reference kept it off as an explicit opt-in.
The knob is still honoured: setting BRIEF_BUILD_PITCH=False in a
deployment that wants the opt-in posture still suppresses the
pitch. This test pins the DEFAULT (module-level constant, no
env/config override) to True.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # 自适应 bootstrap: plugin 根

from code_map import config


def test_brief_build_pitch_default_on():
    # The module constant (the deployment default, before any
    # knob/override) must be True — aligned with Go
    # config.go:110 `const BriefBuildPitch = true`.
    assert config.BRIEF_BUILD_PITCH is True, (
        f"BRIEF_BUILD_PITCH default must be True (Go §2.22 align); "
        f"got {config.BRIEF_BUILD_PITCH!r}"
    )
