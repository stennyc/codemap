"""Read-side unindexed-project clarify, owner design (2026-10-13).

Owner's ruling (this session): the "read an un-indexed project -> ask the
user whether to build a code map" question is a TURN-BOUNDARY decision,
delivered ONCE per project, as a clarify with THREE fixed options:

  要（建索引） / 不要（这次不建） / 不再问（永不再就该项目询问）

The per-read `transform_tool_result` footer (the "抢跑" mid-turn nudge that
a model switch to terminal/execute_code bypasses entirely) is RETIRED for
the clarify-trigger role: reading a source file outside any confirmed
target now records the file's ENCLOSEING DIRECTORY (project boundary),
not the file itself, into a session-scoped ledger
(``data_dir()/_unindexed_read_ledger``), and the turn-boundary
``pre_llm_call`` face (host hook, mirrors filemem's auto-retrieve return
shape) is where the question is delivered.

Semantics nailed down with the owner:
  - Judgment key = DIRECTORY (project boundary), never the file: N reads
    under one project = 1 ledger line = 1 question.
  - Pre-ask gate: pre_llm_call delivers the notice ONLY if the project is
    STILL unconfirmed + no db record at that moment (the user may have
    confirmed between the read and the next turn -> the row auto-falls
    out, no stale question).
  - Post-answer gate (host's post_tool_call sees the clarify call):
      "要"     -> clear ONLY that project's ledger row (it becomes a
                  confirmed target next turn; the row would auto-fall
                  out anyway, the clear is belt-and-suspenders).
      "不要"   -> clear NOTHING (owner 2026-10-13 exact semantics:
                  not indexing right now — the question stands, the
                  same project is asked again on its next read).
      "不再问" -> clear the ledger row for the project it named AND its
                  session stays permanently silent (row removed = never
                  asked again for that project, ever).
  - Session-scoped (P2, owner's simplification: the 3-choice closes it):
    ledger lines carry a session_id; a session's rows are read only by
    that session's pre_llm_call, so rows never leak across sessions.

Every function below is fail-soft: a ledger hiccup must never break the
read, the clarify, or the LLM call.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.test_hooks import _seed_confirmed, _make_store  # noqa: E402


def _ledger_lines(session_id: str = ""):
    """All ledger lines (optionally filtered to one session)."""
    from code_map.config import data_dir
    p = data_dir() / "_unindexed_read_ledger"
    if not p.exists():
        return []
    out = [l for l in p.read_text().splitlines() if l.strip()]
    if session_id:
        out = [l for l in out if session_id in l]
    return out


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch, request):
    """Fresh HERMES_HOME per test so the ledger never touches production."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    yield home
    from tests import test_hooks
    if test_hooks._seed_bases:
        import shutil
        shutil.rmtree(test_hooks._seed_bases.pop(), ignore_errors=True)


def _make_unconfirmed_project(tmp_path, name="proj2", files=None):
    """A source-file project NOT confirmed and with no db record, carrying a
    `.git` marker so `detect_root` resolves to THIS dir (not $HOME — the
    realistic case the owner described: a real project dir, not a bare tmp
    dir that climbs to /root)."""
    base = tmp_path / name
    files = files or {"__init__.py": "def a():\n    pass\n", "core.py": "def b():\n    pass\n"}
    for rel, text in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    # Marker so detect_root lands on `base`, not $HOME.
    (base / ".git").mkdir(exist_ok=True)
    return base


# ── 记什么：post_tool_call read side logs the DIRECTORY, not the file ─────

def test_read_unconfirmed_project_logs_directory_not_file(tmp_path):
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path)
    f1 = proj / "__init__.py"
    f2 = proj / "core.py"
    hook._post_tool_call("read_file", {"path": str(f1)}, None,
                         status="ok", session_id="s1")
    hook._post_tool_call("read_file", {"path": str(f2)}, None,
                         status="ok", session_id="s1")
    lines = _ledger_lines()
    joined = "\n".join(lines)
    # Exactly ONE line, keyed by the DIRECTORY — not one per file.
    assert len(lines) == 1, f"same-dir reads must coalesce to one row: {lines!r}"
    assert str(proj) in joined
    assert str(f1) not in joined.replace(str(proj), "") or str(proj) in joined
    # The individual file paths must NOT appear as rows.
    for line in lines:
        assert f"{f1.name}\t" not in line.split("\t")[0]


def test_read_confirmed_project_does_not_log(tmp_path):
    from code_map import hook
    target = _seed_confirmed(tmp_path, ["e.py"], "def e(): pass\n")
    hook._post_tool_call("read_file", {"path": f"{target}/e.py"}, None,
                         status="ok", session_id="s1")
    assert _ledger_lines() == []


def test_read_non_source_unconfirmed_does_not_log(tmp_path):
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path, files={"notes.md": "# hi\n"})
    hook._post_tool_call("read_file", {"path": str(proj / "notes.md")}, None,
                         status="ok", session_id="s1")
    assert _ledger_lines() == []


def test_write_side_tools_do_not_log(tmp_path):
    """write_file/patch are not read tools; they must not touch the ledger
    (the write side's job is reindex-under-confirmed, already tested in
    test_hooks.py 件1)."""
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path)
    hook._post_tool_call("write_file", {"path": str(proj / "new.py")},
                        {"status": "ok"}, status="ok", session_id="s1")
    assert _ledger_lines() == []


# ── 读什么：pre_llm_call 交付 = 三条固定选项的 clarify 指令 ──────────────

def test_pre_llm_call_delivers_three_option_clarify(tmp_path):
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path)
    hook._post_tool_call("read_file", {"path": str(proj / "core.py")}, None,
                         status="ok", session_id="s1")
    out = hook.on_pre_llm_call(session_id="s1", user_message="继续",
                               conversation_history=[], model="m", platform="tui")
    assert out is not None, "pending unconfirmed project must surface at turn boundary"
    ctx = out["context"]
    assert str(proj) in ctx, "the notice must name the project directory"
    # Owner's three options, in order: 要 / 不要 / 不再问.
    assert "要" in ctx and "不要" in ctx and "不再问" in ctx
    assert "clarify" in ctx, "must instruct the model to call the host clarify tool"


def test_pre_llm_call_nothing_pending_returns_none(tmp_path):
    from code_map import hook
    out = hook.on_pre_llm_call(session_id="s1", user_message="hi",
                               conversation_history=[], model="m", platform="tui")
    assert out is None


def test_pre_llm_call_other_sessions_rows_not_delivered(tmp_path):
    """Session-scoped: rows minted under s1 must NOT surface for s2."""
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path)
    hook._post_tool_call("read_file", {"path": str(proj / "core.py")}, None,
                         status="ok", session_id="s1")
    out = hook.on_pre_llm_call(session_id="s2", user_message="hi",
                               conversation_history=[], model="m", platform="tui")
    assert out is None


def test_pre_llm_call_project_confirmed_between_read_and_turn_drops_row(tmp_path):
    """Pre-ask gate: if the user confirmed the project after the read, the
    pending row falls out — no stale question."""
    from code_map import hook, rootreg
    proj = _make_unconfirmed_project(tmp_path)
    hook._post_tool_call("read_file", {"path": str(proj / "core.py")}, None,
                         status="ok", session_id="s1")
    # Simulate the user confirming (via the code_map_check_root path) — the
    # row must no longer be pending.
    rootreg.write_confirmed_root(str(proj))
    out = hook.on_pre_llm_call(session_id="s1", user_message="继续",
                               conversation_history=[], model="m", platform="tui")
    assert out is None


# ── 答完即清：clarify 应答的三选项各自清账 ──────────────────────────────

def _pending_rows():
    return _ledger_lines("s1")


def test_clarify_reply_yes_clears_named_project_row(tmp_path):
    from code_map import hook
    proj_a = _make_unconfirmed_project(tmp_path, name="projA")
    proj_b = _make_unconfirmed_project(tmp_path, name="projB")
    for p in (proj_a, proj_b):
        hook._post_tool_call("read_file", {"path": str(p / "core.py")}, None,
                             status="ok", session_id="s1")
    assert len(_pending_rows()) == 2
    # User is asked about projA specifically and taps 要.
    hook._post_tool_call("clarify", {"questions": [
        {"question": f"要 index {proj_a} 吗？", "choices": ["要", "不要", "不再问"]}]},
        {"responses": [{"user_response": "要"}]}, status="ok", session_id="s1")
    rows = _pending_rows()
    joined = "\n".join(rows)
    assert str(proj_a) not in joined, "要 -> that project's row is cleared"
    assert str(proj_b) in joined, "the OTHER project's row must survive"


def test_clarify_reply_no_keeps_all_rows(tmp_path):
    """Owner semantics (exact): '不要' = not indexing RIGHT NOW, the question
    stands — nothing is cleared, the SAME project is asked again on its
    next read."""
    from code_map import hook
    proj_a = _make_unconfirmed_project(tmp_path, name="projA")
    proj_b = _make_unconfirmed_project(tmp_path, name="projB")
    for p in (proj_a, proj_b):
        hook._post_tool_call("read_file", {"path": str(p / "core.py")}, None,
                             status="ok", session_id="s1")
    hook._post_tool_call("clarify", {"questions": [
        {"question": f"要 index {proj_a} 吗？", "choices": ["要", "不要", "不再问"]}]},
        {"responses": [{"user_response": "不要"}]}, status="ok", session_id="s1")
    joined = "\n".join(_pending_rows())
    assert str(proj_a) in joined, "不要 -> that project's row stays (asked again)"
    assert str(proj_b) in joined, "不要 -> no other row is touched either"


def test_clarify_reply_neverask_clears_named_project_row(tmp_path):
    from code_map import hook
    proj_a = _make_unconfirmed_project(tmp_path, name="projA")
    proj_b = _make_unconfirmed_project(tmp_path, name="projB")
    for p in (proj_a, proj_b):
        hook._post_tool_call("read_file", {"path": str(p / "core.py")}, None,
                             status="ok", session_id="s1")
    hook._post_tool_call("clarify", {"questions": [
        {"question": f"要 index {proj_a} 吗？", "choices": ["要", "不要", "不再问"]}]},
        {"responses": [{"user_response": "不再问"}]}, status="ok", session_id="s1")
    joined = "\n".join(_pending_rows())
    assert str(proj_a) not in joined, "不再问 -> that project is never asked again"
    assert str(proj_b) in joined, "only the named project is silenced"


def test_clarify_failure_keeps_rows(tmp_path):
    """A cancelled/errored clarify call is NOT an answer — nothing is
    cleared, the next turn asks again."""
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path)
    hook._post_tool_call("read_file", {"path": str(proj / "core.py")}, None,
                         status="ok", session_id="s1")
    hook._post_tool_call("clarify", {"questions": [
        {"question": f"要 index {proj} 吗？", "choices": ["要", "不要", "不再问"]}]},
        {"responses": [{"user_response": "要"}]}, status="error", session_id="s1")
    assert len(_pending_rows()) == 1


# ── reviewer round 1 follow-ups (2026-10-13) ─────────────────────────────

def test_clarify_multi_question_code_map_second(tmp_path):
    """Reviewer round 1: a bundled multi-question clarify where the
    code_map question comes SECOND — reading only responses[0] used to
    miss the code_map answer and leave the row stuck."""
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path)
    hook._post_tool_call("read_file", {"path": str(proj / "core.py")}, None,
                         status="ok", session_id="s1")
    hook._post_tool_call("clarify", {"questions": [
        {"question": "完全无关的另一个话题？", "choices": ["A", "B"]},
        {"question": f"要 index {proj} 吗？", "choices": ["要", "不要", "不再问"]}]},
        {"responses": [
            {"user_response": "A"},
            {"user_response": "要"}]},
        status="ok", session_id="s1")
    joined = "\n".join(_pending_rows())
    assert str(proj) not in joined, "second question's 要 must settle the row"


def test_clarify_multi_project_question_clears_all_named(tmp_path):
    """Reviewer round 1: the model bundles two projects into ONE question
    — both rows must settle (all mentions, not just the longest)."""
    from code_map import hook
    proj_a = _make_unconfirmed_project(tmp_path, name="projA")
    proj_b = _make_unconfirmed_project(tmp_path, name="projB")
    for p in (proj_a, proj_b):
        hook._post_tool_call("read_file", {"path": str(p / "core.py")}, None,
                             status="ok", session_id="s1")
    hook._post_tool_call("clarify", {"questions": [
        {"question": f"要 index {proj_a} 还是 {proj_b} 吗？",
         "choices": ["要", "不要", "不再问"]}]},
        {"responses": [{"user_response": "要"}]}, status="ok", session_id="s1")
    assert _pending_rows() == [], "all named projects must settle"


def test_file_mention_under_pending_boundary_settles_the_row(tmp_path):
    """Reviewer round 1: the model names a FILE under the pending boundary
    ('要 index .../core.py 吗') — the ledger row is keyed by DIRECTORY, so
    the file mention must map up to the boundary row, not no-op."""
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path)
    hook._post_tool_call("read_file", {"path": str(proj / "core.py")}, None,
                         status="ok", session_id="s1")
    assert len(_pending_rows()) == 1
    hook._post_tool_call("clarify", {"questions": [
        {"question": f"要 index {proj / 'core.py'} 吗？",
         "choices": ["要", "不要", "不再问"]}]},
        {"responses": [{"user_response": "要"}]}, status="ok", session_id="s1")
    assert _pending_rows() == [], "file mention under the boundary must settle it"


def test_yes_free_text_prefix_settles(tmp_path):
    """'要建吧' (free-text 要-prefix) settles; '要不要都行' also settles as
    要 (fail-soft direction: over-settle clears a row that would auto-drop
    anyway once the project is confirmed — under-settle would re-ask)."""
    from code_map import hook
    for label, reply in (("a", "要建吧"), ("b", "要不要都行")):
        proj = _make_unconfirmed_project(tmp_path, name=f"proj{label}")
        hook._post_tool_call("read_file", {"path": str(proj / "core.py")}, None,
                             status="ok", session_id="s1")
        hook._post_tool_call("clarify", {"questions": [
            {"question": f"要 index {proj} 吗？",
             "choices": ["要", "不要", "不再问"]}]},
            {"responses": [{"user_response": reply}]},
            status="ok", session_id="s1")
        joined = "\n".join(_pending_rows())
        assert str(proj) not in joined, f"{reply!r} must settle as 要"


def test_neverask_free_text_suffix_settles(tmp_path):
    """'不再问了 谢谢' (prefix match) settles the named project."""
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path)
    hook._post_tool_call("read_file", {"path": str(proj / "core.py")}, None,
                         status="ok", session_id="s1")
    hook._post_tool_call("clarify", {"questions": [
        {"question": f"要 index {proj} 吗？",
         "choices": ["要", "不要", "不再问"]}]},
        {"responses": [{"user_response": "不再问了 谢谢"}]},
        status="ok", session_id="s1")
    assert _pending_rows() == []


def test_ledger_garbage_lines_fail_soft(tmp_path):
    """Reviewer round 1: a hand-corrupted ledger (no-tab garbage lines)
    must not break on_pre_llm_call — malformed lines are skipped."""
    from code_map import hook
    from code_map.config import data_dir
    p = data_dir() / "_unindexed_read_ledger"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("garbage-no-tab\nfoo\tbar baz\n\n", encoding="utf-8")
    out = hook.on_pre_llm_call(session_id="s1", user_message="hi",
                               conversation_history=[], model="m", platform="tui")
    # No well-formed 'proj\tsession' line survives -> nothing pending.
    assert out is None


def test_boundary_never_expands_to_home(tmp_path, monkeypatch):
    """Reviewer round 1: a marker-less project under $HOME must NOT get
    $HOME itself as its boundary (detect_root's $HOME cap would swallow
    the whole home tree — gate ⑩'s recorded 382k-file incident shape).
    The boundary is the file's parent dir instead."""
    from code_map import hook
    # Point $HOME at a tmp dir the test fully controls.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows symmetry
    proj = fake_home / "someproject"
    f = proj / "sub" / "c.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("def c(): pass\n", encoding="utf-8")
    boundary = hook._project_boundary(str(f))
    assert boundary == str(proj / "sub"), (
        f"boundary must be the file's parent dir, not $HOME: {boundary}")
    assert str(fake_home) != boundary
