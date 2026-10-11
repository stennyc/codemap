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
import os
import sys
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # 自适应 bootstrap: plugin 根
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
    from code_map import state as _state
    _state.join_live_build_threads(timeout=5.0)  # let any in-flight build thread finish before we rmtree its db dir
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


def _aff_case(reply: str, expect: bool, monkeypatch, tag: str) -> None:
    """Record `reply` for a fresh confirmed target, then yes=true must
    be build_started iff `expect`."""
    from code_map import rootreg
    from code_map.query import record_clarify_evidence
    home = _mk_home(tag)
    monkeypatch.setenv("HERMES_HOME", str(home))
    proj = _mk_proj(tag)
    (proj / "a.py").write_text("def fa(): pass\n")
    rootreg.write_confirmed_root(str(proj))
    record_clarify_evidence(str(proj), reply)
    res = json.loads(code_map_check_root(
        {"root": "/tmp", "path": proj.name, "yes": True}))
    if expect:
        assert res["status"] == "build_started", (reply, res)
    else:
        assert res["status"] == "blocked_by_policy", (reply, res)
    _cleanup(home, proj)


@pytest.mark.parametrize("reply,expect", [
    # --- RISK #5: previously-missed affirmative phrasings now pass ---
    ("sure", True),
    ("Sure, go ahead", True),
    ("Sounds good", True),
    ("sounds good, yep", True),
    ("可以，动手吧", True),
    ("嗯，好", True),
    ("好，就按这个来", True),
    ("Yep", True),
    ("OK 吧", True),
    ("确认", True),  # existing exact word (regression anchor)
    ("Yes", True),
    # --- still blocked: negations / hedges / neutral ---
    ("不确认", False),
    ("不可以", False),
    ("再想想，先不动手", False),
    ("取消", False),
    ("no", False),
    ("nope", False),
    ("no, skip it", False),
    ("", False),
    ("随便吧", False),
])
def test_check_root_affirmative_wordlist(monkeypatch, reply: str, expect: bool):
    _aff_case(reply, expect, monkeypatch, "aff")


def test_check_root_gate_passes_on_partial_path_evidence(monkeypatch):
    """RISK #6 fallback: evidence recorded under the full target abspath
    must be found by the gate for the SAME target even when the original
    clarify question only mentioned a TRAILING sub-path of it (the model
    said 'plugins/code_map' instead of the full '/root/.hermes/...').

    The hook's target_match partial-path rule is what makes the record
    land under the full abspath; this test drives the public API
    directly (record_clarify_evidence under the joined target) and
    asserts the gate accepts it — closing the silent-blocked gap for
    the exact target. (Hook-level partial-mention coverage lives in
    test_check_root.py::test_hook_records_evidence_via_partial_path_mention.)
    """
    from code_map import rootreg
    from code_map.query import record_clarify_evidence, target_match
    home = _mk_home("q3partial")
    monkeypatch.setenv("HERMES_HOME", str(home))
    proj = _mk_proj("q3partial")
    (proj / "a.py").write_text("def fa(): pass\n")
    # A deeper target so a genuine trailing-sub-path mention exists
    # (a flat /tmp/cm_check_root_q3partial has only 2 path segments, so its
    # only >= 2-segment trailing run IS the full path — the partial-match
    # fallback has no room to bite there; that case is covered by the
    # hook-level test which seeds a real 3-level dir).
    deep = proj / "plugins" / "code_map"
    deep.mkdir(parents=True)
    (deep / "a.py").write_text("def fa(): pass\n")
    rootreg.write_confirmed_root(str(deep))
    # sanity: mentioning just the trailing 2 segments ('plugins/code_map')
    # must match the confirmed target (the RISK #6 fallback)
    q = "要 index 一下 plugins/code_map 吗？"
    assert target_match(q, str(deep)) is True, "trailing-subpath mention must match"
    record_clarify_evidence(str(deep), "确认")
    res = json.loads(code_map_check_root(
        {"root": "/tmp", "path": f"{proj.name}/plugins/code_map", "yes": True}))
    assert res["status"] == "build_started", res
    _cleanup(home, proj)


def test_check_root_evidence_scoped_per_target(monkeypatch):
    """RISK #6 boundary: evidence for target A must NOT unlock target B
    (no cross-target bleed; exact-target scoping is preserved)."""
    from code_map import rootreg
    from code_map.query import record_clarify_evidence
    home = _mk_home("q3scoped")
    monkeypatch.setenv("HERMES_HOME", str(home))
    pa = _mk_proj("q3scoped_a")
    pb = _mk_proj("q3scoped_b")
    (pa / "a.py").write_text("def fa(): pass\n")
    (pb / "a.py").write_text("def fa(): pass\n")
    rootreg.write_confirmed_root(str(pa))
    rootreg.write_confirmed_root(str(pb))
    record_clarify_evidence(str(pa), "确认")  # consent for A only
    resa = json.loads(code_map_check_root(
        {"root": "/tmp", "path": pa.name, "yes": True}))
    resb = json.loads(code_map_check_root(
        {"root": "/tmp", "path": pb.name, "yes": True}))
    assert resa["status"] == "build_started", resa
    assert resb["status"] == "blocked_by_policy", resb
    _cleanup(home, pa, pb)


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


def test_hook_records_evidence_via_partial_path_mention(monkeypatch):
    """RISK #6 fallback at the HOOK level: the question need not spell the
    full abspath. Mentioning a TRAILING sub-path (>= 2 consecutive path
    segments, e.g. 'plugins/code_map' for target .../plugins/code_map) is
    enough for the join — that record still lands under the target's full
    abspath, which is exactly what the gate's per-target lookup expects.

    (Before this fallback the hook only matched `target in question`,
    so a model that said 'plugins/code_map' recorded NOTHING and the
    later yes=true was silently blocked.)"""
    from code_map import hook, rootreg
    from code_map.query import _evidence_log
    home = _mk_home("q3hookpartial")
    monkeypatch.setenv("HERMES_HOME", str(home))
    # 3-segment tail so the partial mention is unambiguous
    proj = _mk_proj("q3hookpartial")
    proj_dir = proj / "plugins" / "code_map"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "a.py").write_text("def fa(): pass\n")
    rootreg.write_confirmed_root(str(proj_dir))
    target = str(proj_dir)

    # verbatim full-path mention still works (regression anchor)
    hook._post_tool_call("clarify",
                         {"question": f"要 index {target} 吗？",
                          "choices": ["确认", "取消"]},
                         result=json.dumps({"response": "确认"}), status="ok")
    recs = [json.loads(l) for l in _evidence_log().read_text().splitlines()]
    assert recs and recs[-1]["target"] == target and recs[-1]["affirmative"] is True

    # now: fresh log, question mentions only the trailing 2 segments
    _evidence_log().unlink()
    hook._post_tool_call("clarify",
                         {"question": "要 index 一下 plugins/code_map 吗？",
                          "choices": ["确认", "取消"]},
                         result=json.dumps({"response": "确认"}), status="ok")
    recs = [json.loads(l) for l in _evidence_log().read_text().splitlines()]
    assert len(recs) == 1, "partial-path mention must still record"
    assert recs[0]["target"] == target, "record lands under the FULL abspath"
    assert recs[0]["affirmative"] is True

    # negative boundary: a question that mentions only a single LAST
    # segment alone ('code_map') is NOT enough (>= 2 consecutive segments
    # required; also 'code_map' alone is collision-prone on its own)
    _evidence_log().unlink()
    hook._post_tool_call("clarify",
                         {"question": "要 index code_map 吗？",
                          "choices": ["确认", "取消"]},
                         result=json.dumps({"response": "确认"}), status="ok")
    assert not _evidence_log().exists(), "single-segment mention must not record"

    _cleanup(home, proj)


def test_hook_new_path_fallback_unlocks_unconfirmed_target(monkeypatch):
    """RISK #6 follow-up (found in the 2026-10-11 knowledge-base-search
    live test): a clarify question about a path that is NOT yet a
    confirmed target must still be able to mint evidence for it, or the
    gate dead-locks (to confirm a new path you need evidence; to get
    evidence you need to have asked about that path; the question, by
    definition, comes before the confirm).

    Boundary: an ABSOLUTE, fully-spelled path mention in the question
    unlocks the join for an unconfirmed target. A RELATIVE fragment
    ('plugins/x') must NOT — that is the collision risk that made the
    first, too-loose fix wrong."""
    from code_map import hook, rootreg
    from code_map.query import _evidence_log
    home = _mk_home("q3newpath")
    monkeypatch.setenv("HERMES_HOME", str(home))
    # seed ONE confirmed target that shares NO trailing-segment overlap
    # with new_dir (a sibling, not an ancestor/parent) so that a relative
    # fragment of new_dir cannot match it via the confirmed-targets arm
    known = _mk_proj("q3newpath_known")
    (known / "a.py").write_text("def fk(): pass\n")
    rootreg.write_confirmed_root(str(known))

    # fresh, empty evidence log before we start asking — the log file may
    # already exist with stale records from a sibling test (the confirm
    # target's own data_dir), so _cleanup the log file itself up front
    _evidence_log().unlink(missing_ok=True)

    new_dir = _mk_proj("q3newpath_new")
    (new_dir / "a.py").write_text("def fn(): pass\n")
    assert not rootreg.is_confirmed_root(str(new_dir))
    abs_new = os.path.abspath(str(new_dir))
    # new_dir lives under the SAME flat parent as known; the relative
    # fragment form is <parent_basename>/<new_dir_basename> — it does NOT
    # share a trailing-segment run with `known` (a different leaf under
    # the same parent), so the confirmed-targets arm cannot match it.
    frag = f"{new_dir.parent.name}/{new_dir.name}"

    hook._post_tool_call("clarify",
                         {"question": f"要 index 一下 {frag} 吗？",
                          "choices": ["确认", "取消"]},
                         result=json.dumps({"response": "确认"}), status="ok")
    # The confirmed-targets arm found nothing (frag shares no trailing
    # segments with `known`); the new-path arm must NOT fire for a
    # relative fragment of a never-seen path.
    assert not _evidence_log().exists(), \
        "relative fragment must not mint evidence for a new path"

    hook._post_tool_call("clarify",
                         {"question": f"要 index {abs_new} 吗？",
                          "choices": ["确认", "取消"]},
                         result=json.dumps({"response": "确认"}), status="ok")
    recs = [json.loads(l) for l in _evidence_log().read_text().splitlines()]
    assert any(r["target"] == abs_new and r["affirmative"] for r in recs)

    # gate contract: root is the boundary prefix, path the relative
    # sub-path — new_dir lives directly under /tmp so (root='/tmp',
    # path=new_dir.name) is the correct pair shape.
    res = json.loads(code_map_check_root(
        {"root": str(new_dir.parent), "path": new_dir.name, "yes": True}))
    assert res["status"] == "build_started", res
    assert rootreg.is_confirmed_root(abs_new)

    _cleanup(home, known, new_dir)


def test_hook_new_path_fallback_never_crosses_unrelated_paths(monkeypatch):
    """The new-path fallback must NOT let an absolute mention of path A
    mint evidence for an unrelated path B (different tree)."""
    from code_map import hook, rootreg
    from code_map.query import _evidence_log
    home = _mk_home("q3newpath2")
    monkeypatch.setenv("HERMES_HOME", str(home))
    a = _mk_proj("q3n2_a")
    (a / "a.py").write_text("def fa(): pass\n")
    rootreg.write_confirmed_root(str(a))

    b = _mk_proj("q3n2_b")
    (b / "a.py").write_text("def fb(): pass\n")
    abs_b = os.path.abspath(str(b))
    assert abs_b != os.path.abspath(str(a))

    hook._post_tool_call("clarify",
                         {"question": f"要 index {abs_b} 吗？",
                          "choices": ["确认", "取消"]},
                         result=json.dumps({"response": "确认"}), status="ok")
    recs = [json.loads(l) for l in _evidence_log().read_text().splitlines()]
    targets = {r["target"] for r in recs}
    assert abs_b in targets, f"new-path absolute mention must record, got {targets}"
    a_target = os.path.abspath(str(a))
    a_recs = [r for r in recs if r["target"] == a_target]
    assert not a_recs, f"unrelated confirmed target gained spurious evidence: {a_recs}"

    _cleanup(home, a, b)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
