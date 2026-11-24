"""L2.8h gate-② regression: the turn-boundary build pitch is DEFAULT-OFF
(owner decision ③, 2026-11-18 — stable-injection premise: a per-turn
variable injection is a prefix-cache enemy; the pitch becomes an explicit
opt-in via config.BRIEF_BUILD_PITCH).

Isolation mirrors test_turn_boundary_clarify.py: a fresh HERMES_HOME per
test so the ledger never touches production.
"""
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
for p in (str(PLUGIN_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from code_map import config as cm_config
from code_map import hook as cm_hook


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Fresh HERMES_HOME per test so the ledger never touches production."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    yield home


def _make_unconfirmed_project(tmp_path, name="projGate2"):
    """A source-file project NOT confirmed, with a `.git` marker so
    detect_root resolves to THIS dir (the realistic unindexed-project
    shape the pitch was built for)."""
    base = tmp_path / name
    base.mkdir()
    (base / ".git").mkdir()
    (base / "core.py").write_text("def b():\n    pass\n", encoding="utf-8")
    return base


def _mint_ledger_row(tmp_path, session_id="s1"):
    proj = _make_unconfirmed_project(tmp_path)
    cm_hook._post_tool_call(
        "read_file", {"path": str(proj / "core.py")}, None,
        status="ok", session_id=session_id)
    return proj


def test_gate_2_default_off_skips_pitch(tmp_path, monkeypatch):
    """Pending ledger row + BRIEF_BUILD_PITCH=False (opt-out) ->
    on_pre_llm_call returns None even though the project is unindexed.

    §2.22 (2026-11-24, aligns Go config.go:110): the default is now
    True; this test pins the OPT-OUT posture by monkeypatching the
    flag back to False.
    """
    monkeypatch.setattr(cm_config, "BRIEF_BUILD_PITCH", False, raising=False)
    _mint_ledger_row(tmp_path, "s1")
    out = cm_hook.on_pre_llm_call(session_id="s1")
    assert out is None, "the pitch must not fire while the flag is off"


def test_gate_2_default_on_surfaces_pitch(tmp_path):
    """§2.22: BRIEF_BUILD_PITCH default is True — a pending row
    surfaces as the clarify instruction WITHOUT any monkeypatch.
    Mirrors Go config.go:110 `const BriefBuildPitch = true`."""
    assert cm_config.BRIEF_BUILD_PITCH is True, "default must be ON (Go §2.22 align)"
    _mint_ledger_row(tmp_path, "s3")
    out = cm_hook.on_pre_llm_call(session_id="s3")
    assert out is not None, "default-ON pitch must fire"
    ctx = out.get("context", "")
    assert "clarify" in ctx
    assert "不再问" in ctx


def test_gate_2_opt_in_restores_pitch(tmp_path, monkeypatch):
    """Flipping BRIEF_BUILD_PITCH=True restores the pitch path: the
    pending row surfaces as the clarify instruction (要 / 不要 / 不再问)."""
    _mint_ledger_row(tmp_path, "s2")
    monkeypatch.setattr(cm_config, "BRIEF_BUILD_PITCH", True, raising=False)
    out = cm_hook.on_pre_llm_call(session_id="s2")
    assert out is not None, "opt-in must restore the pitch"
    ctx = out.get("context", "")
    assert "clarify" in ctx
    assert "不再问" in ctx
