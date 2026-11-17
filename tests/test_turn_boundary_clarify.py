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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "code_map"))  # tests/ 包可导入（从任一 cwd 跑测试）

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


def test_read_under_a_later_confirmed_project_purges_its_stale_row(tmp_path):
    """Owner 2026-10-19: a ledger row written BEFORE the project was
    confirmed (the read-time boundary was a sub-dir that was not itself
    confirmed) becomes the REFRESH case once an ancestor project is
    confirmed — the next read under it must purge the stale row so the
    turn-boundary question cannot resurrect, and on_pre_llm_call must
    stay silent for the session."""
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path, name="projP")
    f = proj / "core.py"
    # 1) read before confirmation -> one ledger row for the project.
    hook._post_tool_call("read_file", {"path": str(f)}, None,
                         status="ok", session_id="s1")
    rows = _pending_rows()
    assert len(rows) == 1 and str(proj) in rows[0], rows
    # 2) the project is now confirmed (and indexed) while the stale row lingers.
    from code_map import rootreg
    from code_map.state import _run_build
    from code_map.fingerprint import invalidate_cache
    rootreg.write_confirmed_root(str(proj))
    _run_build(str(proj))
    rootreg.clear_dirty(str(proj))
    invalidate_cache(str(proj))
    # 3) the turn-boundary face must not re-ask (filter at read time).
    out = hook.on_pre_llm_call(session_id="s1", user_message="hi",
                               conversation_history=[], model="m",
                               platform="tui")
    assert out is None, f"stale row under a confirmed target must not ask: {out}"
    # 4) the next read purges the stale ledger row itself.
    hook._post_tool_call("read_file", {"path": str(f)}, None,
                         status="ok", session_id="s1")
    assert _pending_rows() == [], "the stale row must be purged by the read"


def test_on_pre_llm_call_skips_projects_under_a_confirmed_target(tmp_path):
    """Owner 2026-10-19 (filter face): a manually-seeded ledger row whose
    project sits UNDER a confirmed target must be skipped by
    on_pre_llm_call even before any purge read happens — that is the
    refresh case, never the '是否建索引' case."""
    from code_map import hook
    from code_map.config import data_dir
    target = _seed_confirmed(tmp_path, ["top.py"], "def top(): pass\n")
    sub = target + "/sub"
    data_dir().mkdir(parents=True, exist_ok=True)
    p = data_dir() / "_unindexed_read_ledger"
    p.write_text(f"{sub}\ts1\n", encoding="utf-8")
    out = hook.on_pre_llm_call(session_id="s1", user_message="hi",
                               conversation_history=[], model="m",
                               platform="tui")
    assert out is None, f"row under a confirmed target must be skipped: {out}"


def test_purge_is_noop_for_genuinely_pending_project(tmp_path):
    """Owner-awareness (reviewer round 1, P2): the purge must NOT clear a
    row for a project that is still genuinely pending (unconfirmed, not
    under a confirmed target, no db record) — the '不要 = keep asking'
    semantics require the row to survive its own re-read."""
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path, name="projQ")
    f = proj / "core.py"
    hook._post_tool_call("read_file", {"path": str(f)}, None,
                         status="ok", session_id="s1")
    assert len(_pending_rows()) == 1
    # Re-reading the SAME pending project must leave the row intact.
    hook._post_tool_call("read_file", {"path": str(f)}, None,
                         status="ok", session_id="s1")
    assert _pending_rows() == [f"{proj}\ts1"], \
        "purge must be a no-op for a genuinely pending project"
    # The question must still be offered next turn.
    out = hook.on_pre_llm_call(session_id="s1", user_message="hi",
                               conversation_history=[], model="m",
                               platform="tui")
    assert out is not None and str(proj) in out["context"]


def test_read_non_source_unconfirmed_logs_directory(tmp_path):
    """Owner 2026-10-18 (defect 2): the clarify offer is DIRECTORY-triggered,
    not source-file-triggered — reading ANY file (e.g. .md) inside an
    un-indexed project logs the project row, so the turn-boundary clarify
    can propose indexing it. (The legacy behaviour was 'non-source reads
    do NOT log', which is what this test now inverts.)"""
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path, files={"notes.md": "# hi\n"})
    hook._post_tool_call("read_file", {"path": str(proj / "notes.md")}, None,
                         status="ok", session_id="s1")
    lines = _ledger_lines()
    assert len(lines) == 1, f"non-source read under un-indexed project must log its dir: {lines!r}"
    assert str(proj) in lines[0]


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


# ── reviewer round 2 follow-ups (2026-10-13) ─────────────────────────────

def test_same_process_threads_no_row_loss(tmp_path):
    """R2-1: two bounded worker THREADS in the same host process used to
    lose ~99% of rows (flock on per-thread fds does NOT serialize
    cross-thread). The threading.Lock in _ledger_rmw closes that hole:
    N threads x M appends must yield exactly the distinct rows."""
    import threading
    from code_map import hook
    from code_map.config import data_dir
    proj = _make_unconfirmed_project(tmp_path)
    errors = []

    def worker(i):
        row = f"p{i}\ts{i}"

        def append(cur, r=row):
            if r in cur:
                return None
            cur.append(r)
            return cur

        for _ in range(200):
            try:
                hook._ledger_rmw(append)
            except Exception as e:  # pragma: no cover
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"RMW must not raise: {errors[:3]}"
    p = data_dir() / "_unindexed_read_ledger"
    lines = p.read_text().splitlines() if p.exists() else []
    uniq = set(l.split("\t")[0] for l in lines)
    assert len(uniq) == 4, f"4 distinct threads -> 4 rows, got {uniq!r}"
    assert len(lines) == 4, f"each thread's row coalesces to one: {lines!r}"


def test_affirmative_yes_word_clears_q3_gate(tmp_path):
    """Owner's '要' is a fixed clarify choice — _is_affirmative must treat
    the bare word '要' (and its free-text 要-prefix variants) as
    affirmative, otherwise the '要' flow dead-ends at the Q3 evidence
    gate (record_clarify_evidence would mint an affirmative:false line)."""
    from code_map.query import _is_affirmative
    for reply in ("要", "要了", "要建立", "要建吧", "要建索引"):
        assert _is_affirmative(reply), f"{reply!r} must be affirmative"
    for reply in ("不要", "不再问", "不用", "否"):
        assert not _is_affirmative(reply), f"{reply!r} must NOT be affirmative"


def test_check_root_under_boundary_evidence_passes_gate(tmp_path):
    """R2 reviewer #1: evidence minted for the boundary dir /x/projA must
    authorize a check_root target that lies UNDER that dir (e.g.
    /x/projA/core.py via root=/x/projA, path=core.py) — the Q3 gate's
    descent relaxation. Without it, the user taps 要 and the build is
    still blocked_by_policy."""
    from code_map import hook, rootreg
    from code_map.query import record_clarify_evidence, _clarify_evidence
    proj = _make_unconfirmed_project(tmp_path, name="projA")
    proj_str = str(proj)
    # Simulate: read -> ledger row -> clarify '要' -> evidence minted for
    # the BOUNDARY dir (what _record_clarify_evidence's new-path fallback
    # actually stores, per the clarify question text).
    record_clarify_evidence(proj_str, "要", session_id="s1")
    # The gate called with target = a file UNDER the boundary must pass.
    sub_target = os.path.join(proj_str, "core.py")
    ev = _clarify_evidence(sub_target, "", "", session_id="s1")
    assert ev is not None, "descent relaxation: a check_root target under " \
                           "the evidence's boundary dir must be accepted"
    # The reverse (wider than the boundary) must NOT be authorized.
    wider = os.path.dirname(proj_str)
    ev2 = _clarify_evidence(wider, "", "", session_id="s1")
    assert ev2 is None, "a target WIDER than the boundary must stay blocked"


def test_check_root_exactly_boundary_passes_gate(tmp_path):
    """The natural split root=<parent>, path=<boundary name> produces a
    check_root target EQUAL to the evidence target — must pass."""
    from code_map import hook
    from code_map.query import record_clarify_evidence, _clarify_evidence
    proj = _make_unconfirmed_project(tmp_path, name="projA")
    record_clarify_evidence(str(proj), "要", session_id="s1")
    ev = _clarify_evidence(str(proj), "", "", session_id="s1")
    assert ev is not None


def test_home_root_level_file_no_boundary(tmp_path, monkeypatch):
    """R2-4 residual: a source file DIRECTLY under $HOME ($HOME/y.py,
    no markers anywhere) must NOT get $HOME itself as its boundary —
    _project_boundary returns None, so no ledger row is ever minted for
    a bare home-root file."""
    from code_map import hook
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    f = fake_home / "y.py"
    f.write_text("def y(): pass\n", encoding="utf-8")
    assert hook._project_boundary(str(f)) is None, (
        "a file at $HOME root is not a project boundary")


def test_search_files_relative_pattern_does_not_log(tmp_path):
    """Reviewer round 1: a bare relative search_files pattern ('*.py')
    abspaths to the CWD — logging the CWD as a 'project' is noise. Only
    absolute patterns (or read_file) are trustworthy triggers."""
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path)
    # Read one file to establish a real row.
    hook._post_tool_call("read_file", {"path": str(proj / "core.py")}, None,
                         status="ok", session_id="s1")
    before = _pending_rows()
    # A relative search_files pattern must NOT add a CWD row.
    hook._post_tool_call("search_files", {"pattern": "core.py"}, None,
                         status="ok", session_id="s1")
    assert _pending_rows() == before, "relative pattern must not mint a row"


# ── deleted-directory guard (2026-11-17): a dead folder is not "pending" ──

def test_pre_llm_call_deleted_project_does_not_ask(tmp_path):
    """A pending ledger row whose project folder was DELETED after the read
    must not fire a build question. Deletion is invisible to the
    confirm / under-confirmed / db guards (a deleted dir is not confirmed,
    sits under no confirmed target, and has no db record), so without the
    existence guard a stale row would ask '是否建索引' forever on a folder
    that no longer exists (the owner's deleted 3rd-copy shape)."""
    import shutil
    from code_map import hook
    proj = _make_unconfirmed_project(tmp_path, name="projDeleted")
    hook._post_tool_call("read_file", {"path": str(proj / "core.py")}, None,
                         status="ok", session_id="s1")
    assert len(_pending_rows()) == 1, "row is pending while the dir is alive"
    # The folder is deleted (owner's 3rd-copy discipline).
    shutil.rmtree(proj, ignore_errors=True)
    out = hook.on_pre_llm_call(session_id="s1", user_message="hi",
                               conversation_history=[], model="m",
                               platform="tui")
    assert out is None, f"deleted project must not fire a build question: {out}"


def test_needs_index_build_question_dead_dir_false_live_true(tmp_path):
    """_needs_index_build_question is the single 'genuinely pending' gate:
    a folder that no longer exists on disk must return False (no question);
    a live unconfirmed, no-db project must still return True (no
    over-suppression of a real pending project)."""
    from code_map import hook
    live = _make_unconfirmed_project(tmp_path, name="projLive")
    dead = str(tmp_path / "proj_gone")  # never created
    assert hook._needs_index_build_question(str(live)) is True, \
        "a live unconfirmed, no-db project must remain genuinely pending"
    assert hook._needs_index_build_question(dead) is False, \
        "a folder that does not exist must not be asked about"


def test_needs_index_build_question_transient_is_false(tmp_path):
    """Owner 2026-11-17 '临时目录永不再问': the ASK gate must treat a LIVE
    transient cache/data dir as NOT genuinely pending (no build question) —
    distinct from the deleted-dir case (suppressed by the isdir guard). A
    live real source project must still be True (no over-suppression)."""
    from code_map import hook
    trans = tmp_path / "cache" / "scratch"
    (trans / ".git").mkdir(parents=True)
    (trans / "f.py").write_text("def f(): pass\n", encoding="utf-8")
    live = _make_unconfirmed_project(tmp_path, name="projLiveT")
    assert hook._needs_index_build_question(str(trans)) is False, \
        "a live transient cache dir must not be offered for indexing"
    assert hook._needs_index_build_question(str(live)) is True, \
        "a live real source project must remain genuinely pending"
