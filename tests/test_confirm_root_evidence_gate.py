"""code_map_confirm_root must enforce the SAME fail-closed transcript-evidence
gate as code_map_check_root(yes=true) for TRULY NEW targets (2026-11-16).

The 382k-file incident closed the check_root surface with a transcript
evidence gate; confirm_root (the 'formal' tool) had NONE — any model call
bypassed the user-nod requirement by using the other tool. Today's
on-disk forensics (a session whose check_root(yes=true) kept returning
blocked_by_policy while code_map_confirm_root passed and started the build)
is the incident shape this gate closes.

Rules:
  - target already in the registry, or a DESCENDANT of a registered target
    -> registry is the persistent authorization record; no fresh evidence
    needed (same exemption as check_root).
  - truly new target -> requires a same-session affirmative clarify
    evidence line for that exact target (or an ancestor boundary, the
    Q3 descent relaxation); absent -> blocked_by_policy + an audit line
    (result=blocked_evidence_miss) so the miss is diagnosable without
    disk archaeology.
  - evidence present -> build started + success audit line carrying
    transcript_evidence (replayable confirmation chain).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest  # noqa: E402

from code_map.query import code_map_confirm_root, record_clarify_evidence  # noqa: E402
from code_map import rootreg  # noqa: E402


def _setup(tmp_path, monkeypatch, tag, proj_files=("a.py",)):
    home = tmp_path / f"home_{tag}"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)  # sessionless degrade path
    parent = tmp_path / f"parent_{tag}"
    parent.mkdir()
    proj = parent / f"proj_{tag}"
    proj.mkdir()
    for name in proj_files:
        (proj / name).write_text("def f(): pass\n")
    audit = home / "plugins" / "code_map" / "data" / "confirm_root_log.jsonl"
    return home, proj, audit


def _lines(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _call(proj: Path) -> dict:
    return json.loads(code_map_confirm_root({
        "root": str(proj.parent), "path": proj.name}))


def test_confirm_root_blocked_without_evidence(tmp_path, monkeypatch):
    home, proj, audit = _setup(tmp_path, monkeypatch, "noev")
    res = _call(proj)
    assert res["status"] == "blocked_by_policy", res
    assert proj.resolve() not in {Path(r) for r in rootreg.confirmed_targets()}, \
        "a refused confirm must NOT register the target"
    misses = [l for l in _lines(audit) if l.get("result") == "blocked_evidence_miss"]
    assert len(misses) == 1, f"refused build must write ONE miss audit line, got {_lines(audit)}"
    assert misses[0]["root"] == str(proj)
    from code_map import state as _state
    _state.join_live_build_threads(timeout=5.0)


def test_confirm_root_passes_with_evidence(tmp_path, monkeypatch):
    home, proj, audit = _setup(tmp_path, monkeypatch, "withev")
    record_clarify_evidence(str(proj), "确认")
    res = _call(proj)
    assert res["status"] == "build_started", res
    assert any(Path(r).resolve() == proj.resolve() for r in rootreg.confirmed_targets()), \
        "confirmed target must land in the registry"
    ok = [l for l in _lines(audit) if l.get("result") not in ("blocked_evidence_miss",)]
    assert len(ok) == 1, f"success audit line missing: {_lines(audit)}"
    assert ok[0]["transcript_evidence"], "success audit must carry the transcript evidence"
    from code_map import state as _state
    _state.join_live_build_threads(timeout=5.0)


def test_confirm_root_non_affirmative_reply_not_evidence(tmp_path, monkeypatch):
    home, proj, audit = _setup(tmp_path, monkeypatch, "neg")
    record_clarify_evidence(str(proj), "再想想")  # non-affirmative
    res = _call(proj)
    assert res["status"] == "blocked_by_policy", res


def test_confirm_root_registry_hit_exempt(tmp_path, monkeypatch):
    """An ALREADY-CONFIRMED target keeps the registry as its authorization
    record — no fresh clarify needed (the 'asked last session, asked again'
    loop exemption, same as check_root)."""
    home, proj, audit = _setup(tmp_path, monkeypatch, "reghit")
    rootreg.write_confirmed_root(str(proj))
    res = _call(proj)
    assert res["status"] == "build_started", res
    from code_map import state as _state
    _state.join_live_build_threads(timeout=5.0)


def test_confirm_root_descendant_of_confirmed_exempt(tmp_path, monkeypatch):
    """A sub-path of a confirmed ancestor is exempt the same way
    (registry ancestry = persistent authorization)."""
    home, proj, audit = _setup(tmp_path, monkeypatch, "desc")
    sub = proj / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("class B: pass\n")
    rootreg.write_confirmed_root(str(proj))  # ancestor confirmed
    res = json.loads(code_map_confirm_root(
        {"root": str(proj), "path": "sub"}))
    assert res["status"] == "build_started", res
    from code_map import state as _state
    _state.join_live_build_threads(timeout=5.0)


def test_confirm_root_gate10_pair_rule_unchanged(tmp_path, monkeypatch):
    """The root+path pair rule (gate 10) still applies first: a root-only
    or path-only call is refused as invalid BEFORE the evidence gate."""
    home, proj, audit = _setup(tmp_path, monkeypatch, "pair")
    res = json.loads(code_map_confirm_root({"root": str(proj)}))
    assert res["status"] == "invalid", res
    res2 = json.loads(code_map_confirm_root({"path": "somewhere"}))
    assert res2["status"] == "invalid", res2
