"""2B code_map_check_root tests: dry-run default, confirm-gated build, jsonl audit.

Run per standing rule:
    cd /root/.hermes/plugins && <py3.14> -m pytest code_map/tests/ -q

Design (borrowed from filemem sweep_unconfirmed.py protocol, not its tool shell):
  - dry-run is the DEFAULT: reports what WOULD be indexed, writes nothing
  - confirm (yes=True) lifts to actually call state.confirm_root
  - every confirm success appends ONE jsonl audit line to data/confirm_root_log.jsonl
    (recoverable trail: who built which root, when, how many files)
  - gate 10 v2 (root+path both mandatory, joined target) applies to the confirm path

v2 (2026-10-11): the check tool now takes a (root, path) pair — root is
a boundary prefix, path the relative sub-path. Test dirs live under
/tmp with a shared parent so the pair shape is exercisable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import code_map.tests  # noqa: F401  (sys.path bootstrap, see tests/__init__.py)

import pytest

from code_map.query import code_map_check_root


def _mk_proj(dir_name: str) -> Path:
    p = Path(f"/tmp/cm_check_root_{dir_name}")
    p.mkdir(exist_ok=True)
    return p


def _mk_home(dir_name: str) -> Path:
    h = Path(f"/tmp/cm_check_home_{dir_name}")
    h.mkdir(exist_ok=True)
    return h


def _audit_lines(home: Path) -> list:
    p = home / "plugins" / "code_map" / "data" / "confirm_root_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _cleanup(*paths: Path) -> None:
    import shutil
    for p in paths:
        try:
            shutil.rmtree(p)
        except OSError:
            pass


def test_check_root_default_is_dry_run(monkeypatch):
    """Default call (no yes) must report preview stats and NOT build / write audit."""
    home = _mk_home("dryrun")
    monkeypatch.setenv("HERMES_HOME", str(home))
    proj = _mk_proj("dryrun")
    (proj / "a.py").write_text("def fa(): pass\n")
    (proj / "b.py").write_text("class Cb:\n    pass\n")

    res = json.loads(code_map_check_root(
        {"root": "/tmp", "path": proj.name}))
    # dry-run: reports count, does NOT start a build, does NOT write audit
    assert res["dry_run"] is True
    assert res["files_indexable"] == 2
    # no audit line written yet
    audit = _audit_lines(home)
    assert audit == [], f"dry-run must not write audit, got {audit}"
    # no db created for this (fresh) target
    from code_map.config import root_db_path
    assert not root_db_path(str(proj)).exists(), "dry-run must not build a db"

    _cleanup(home, proj)


def test_check_root_yes_writes_audit_and_builds(monkeypatch):
    """yes=True must call confirm_root and append exactly one audit line.

    Q3 gate (owner policy 2026-10-11): a yes=true build is only released
    when the transcript has an affirmative clarify reply FOR THIS TARGET.
    The test plays the host: record the evidence the way the
    post_tool_call hook does (the model's clarify call, user said 确认),
    then the gate must pass and the audit line must carry the evidence."""
    from code_map import rootreg
    from code_map.query import record_clarify_evidence
    _cleanup(_mk_home("apply"), _mk_proj("apply"))
    home = _mk_home("apply")
    monkeypatch.setenv("HERMES_HOME", str(home))
    proj = _mk_proj("apply")
    (proj / "a.py").write_text("def fa(): pass\n")
    rootreg.write_confirmed_root(str(proj))

    # No evidence yet -> the gate must REFUSE (fail-closed).
    res0 = json.loads(code_map_check_root(
        {"root": "/tmp", "path": proj.name, "yes": True}))
    assert res0["status"] == "blocked_by_policy", res0
    assert len(_audit_lines(home)) == 0, "refused build writes no audit line"

    # The model asked the user (host clarify) and the user said 确认 ->
    # the host hook records it. Then the gate passes.
    record_clarify_evidence(str(proj), "确认")
    res = json.loads(code_map_check_root(
        {"root": "/tmp", "path": proj.name, "yes": True}))
    assert res["dry_run"] is False
    assert res["status"] == "build_started"

    audit = _audit_lines(home)
    assert len(audit) == 1, f"expect exactly 1 audit line, got {audit}"
    line = audit[0]
    assert line["action"] == "confirm_root"
    assert line["root"] == str(proj)
    assert line["files_indexed"] == 1
    assert "ts" in line and "db_path" in line
    # Q3: the audit line now carries the transcript evidence that
    # justified the build.
    assert "transcript_evidence" in line
    assert line["transcript_evidence"]["affirmative"] is True

    _cleanup(home, proj)


def test_check_root_gate_invalid_pair_refused_even_with_yes(monkeypatch):
    """gate v2: a root-only pair (empty path) must be refused."""
    home = _mk_home("blocked")
    monkeypatch.setenv("HERMES_HOME", str(home))
    res = json.loads(code_map_check_root(
        {"root": "/root", "path": "", "yes": True}))
    assert res["status"] == "invalid"
    # no audit line for a refused build
    audit = _audit_lines(home)
    assert audit == [], "refused pair must not write audit"

    _cleanup(home)


# ── Q3 transcript gate (owner policy 2026-10-11) ──────────────────────────

def test_check_root_blocked_by_policy_without_clarify_evidence(monkeypatch):
    """yes=true with NO clarify evidence for the target -> blocked.

    This is the fail-closed half: a build the user never explicitly
    confirmed (no clarify reply in the transcript) must not start, even
    though the pair passes gate 10 and the target is a valid dir."""
    from code_map import rootreg
    home = _mk_home("q3blocked")
    monkeypatch.setenv("HERMES_HOME", str(home))
    proj = _mk_proj("q3blocked")
    (proj / "a.py").write_text("def fa(): pass\n")
    rootreg.write_confirmed_root(str(proj))

    res = json.loads(code_map_check_root(
        {"root": "/tmp", "path": proj.name, "yes": True}))
    assert res["status"] == "blocked_by_policy", res
    assert "clarify" in res["note"], res["note"]
    # a blocked build must NOT have started a background build and must
    # NOT have written an audit line (nothing was confirmed).
    assert len(_audit_lines(home)) == 0

    # The dry-run path is UNCHANGED by the gate (it never builds): it must
    # still work with no evidence.
    dry = json.loads(code_map_check_root(
        {"root": "/tmp", "path": proj.name}))
    assert dry["dry_run"] is True and dry["files_indexable"] == 1

    _cleanup(home, proj)


def test_check_root_rejects_non_affirmative_reply(monkeypatch):
    """A clarify reply that is NOT an explicit yes (e.g. '再想想' / a
    refusal) must NOT count as evidence — the gate stays blocked
    (conservative: only affirmative words unlock a build)."""
    from code_map import rootreg
    from code_map.query import record_clarify_evidence
    home = _mk_home("q3neg")
    monkeypatch.setenv("HERMES_HOME", str(home))
    proj = _mk_proj("q3neg")
    (proj / "a.py").write_text("def fa(): pass\n")
    rootreg.write_confirmed_root(str(proj))

    record_clarify_evidence(str(proj), "再想想，先不动手")
    res = json.loads(code_map_check_root(
        {"root": "/tmp", "path": proj.name, "yes": True}))
    assert res["status"] == "blocked_by_policy", res

    # A later affirmative reply for the SAME target unlocks it.
    record_clarify_evidence(str(proj), "确认")
    res2 = json.loads(code_map_check_root(
        {"root": "/tmp", "path": proj.name, "yes": True}))
    assert res2["status"] == "build_started", res2
    assert len(_audit_lines(home)) == 1

    _cleanup(home, proj)


def test_clarify_hook_records_evidence_for_mentioned_target(monkeypatch):
    """The post_tool_call hook records a host clarify call as transcript
    evidence ONLY when the question mentions a confirmed target path and
    the reply is affirmative — the join key that lets a later yes=true
    build know which target the user approved."""
    from code_map import hook, rootreg
    from code_map.query import _evidence_log
    home = _mk_home("q3hook")
    monkeypatch.setenv("HERMES_HOME", str(home))
    proj = _mk_proj("q3hook")
    (proj / "a.py").write_text("def fa(): pass\n")
    rootreg.write_confirmed_root(str(proj))
    target = str(proj)

    # A clarify that does NOT mention any confirmed target -> no record.
    hook._post_tool_call("clarify",
                         {"question": "要装个新插件吗？", "choices": ["装", "不装"]},
                         result=json.dumps({"response": "装"}), status="ok")
    assert not _evidence_log().exists(), "non-target clarify records nothing"

    # A clarify that mentions the target but the user declines -> recorded,
    # but as a NON-affirmative record (the gate still blocks).
    hook._post_tool_call("clarify",
                         {"question": f"要 index {target} 吗？", "choices": ["确认", "取消"]},
                         result=json.dumps({"response": "取消"}), status="ok")
    recs = [json.loads(l) for l in _evidence_log().read_text().splitlines()]
    assert len(recs) == 1 and recs[0]["target"] == target
    assert recs[0]["affirmative"] is False

    # ... and an affirmative one -> affirmative=True, which the gate accepts.
    hook._post_tool_call("clarify",
                         {"question": f"要 index {target} 吗？", "choices": ["确认", "取消"]},
                         result=json.dumps({"response": "确认"}), status="ok")
    recs = [json.loads(l) for l in _evidence_log().read_text().splitlines()]
    assert recs[-1]["affirmative"] is True

    # A FAILED/cancelled clarify call must NOT be recorded as consent.
    hook._post_tool_call("clarify",
                         {"question": f"要 index {target} 吗？"},
                         result=json.dumps({"response": "确认"}), status="error")
    recs = [json.loads(l) for l in _evidence_log().read_text().splitlines()]
    assert len(recs) == 2, "an errored clarify is not evidence"

    _cleanup(home, proj)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
