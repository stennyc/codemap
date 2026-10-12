"""hook.py — Tier-1 hooks for code_map auto-maintenance + auto-attach.

Two host hook points, both fail-soft (gate 9: any exception -> no-op,
never block the tool result from reaching the LLM):

  post_tool_call    write/patch side. On a SUCCESSFUL write_file/patch
                    under a confirmed target, re-parse that single file and
                    upsert it (synchronous, ms-scale — correctness over
                    background threads that raced the commit and left the
                    index stale). Marks the target dirty.

  transform_tool_result
                    read side. On read_file/search_files landing on a source
                    file under a confirmed target, AUTO-ATTACH that file's
                    symbol outline (top READ_SELF_SYMBOLS) + its cross-file
                    import edges (top READ_IMPORT_EDGES) to the tool result
                    text, so the model gets project-aware context without a
                    separate code_map_query round-trip. If the target is
                    dirty, a synchronous full rebuild runs first (件 3
                    self-heal), then the attach. Triggers ONLY for
                    INDEXED_EXTENSIONS source files (not .md/.json/binary).
                    If the query itself fails (corrupt db, missing venv),
                    fall back to the plain "prefer code_map_query" nudge —
                    the read result is NEVER broken by a map issue.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

from .config import (HOOK_DEBOUNCE_S, READ_HEAL_MAX_ATTEMPTS, READ_HEAL_MAX_FILES,
                     READ_IMPORT_EDGES, READ_SELF_SYMBOLS,
                     READ_SYMBOL_MAX_BYTES, data_dir)
from . import rootreg

_log = logging.getLogger("code_map.hook")

# Re-entrancy guard for the read-side dirty heal: one lock so two concurrent
# reads of the same target cannot stack two full-walk rebuild workers that
# both mutate the same WAL db (reviewer F1 / item H).
import threading
_heal_lock = threading.Lock()

# Debounce: last schedule time per target, to coalesce burst writes.
# Guarded by `_debounce_lock`: the host fires post_tool_call on a fresh
# bounded worker thread per tool call, so two concurrent writes to the
# same target can both read `_last_schedule` before either writes it —
# the lock makes the check-then-set atomic (reviewer c-1; WAL +
# busy_timeout already keeps the db itself safe, this only prevents the
# double upsert + double mark_dirty on the same file).
_last_schedule: Dict[str, float] = {}
_debounce_lock = threading.Lock()

# Read-side tools that benefit from the auto-attach / nudge.
_READ_TOOLS = ("read_file", "search_files")
_WRITE_TOOLS = ("write_file", "patch")


# ── write/patch side: synchronous single-file reindex (件 1) ────────────────

def _extract_clarify_qr(args: Any, result: Any) -> tuple[Optional[str], str]:
    """C-0: pull (question_text, reply_text) out of a host clarify call.

    The host clarify tool's real schema is ``questions=[{qid, question,
    choices?, multi_select?}]`` (a plural array; each entry is one question,
    UI appends an 'Other' free-text row, answers come back keyed by qid).
    This mirrors the legacy single-shot shape ``{"question", "choices"}``
    (unit tests / earlier host builds) and returns the FIRST question's text
    + the reply for it. If a call carries multiple questions we only mine
    evidence for the first — a multi-question clarify that bundles a code_map
    target with other topics is out of scope for the gate's 1:1 join key.

    Reply extraction (shape-agnostic, keep the old tolerant parsing):
      - result is the post_tool_call payload. Depending on the host renderer
        it may be a JSON string, a dict, or a raw string. We walk the common
        shapes and pull out the first non-empty user response text.
    """
    q: Optional[str] = None
    if isinstance(args, dict):
        qs = args.get("questions")
        if isinstance(qs, list) and qs:
            first = qs[0]
            if isinstance(first, dict):
                q = str(first.get("question") or "").strip() or None
            elif isinstance(first, str):
                q = first.strip() or None
        if not q:
            q = str(args.get("question") or "").strip() or None
    reply = ""
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            parsed = result
    else:
        parsed = result
    if isinstance(parsed, dict):
        # Host clarify_tool._result returns:
        #   {"responses": [{"question", "choices_offered", "status", "user_response"}, ...],
        #    "outcome": "..."}
        # — a LIST of response dicts keyed by qid, each carrying user_response.
        # Also accept legacy shapes: {"responses": {"q0": "..."}} (dict),
        # {"answers": [...]}, {"response": "..."}.
        for k in ("responses", "answers", "response"):
            v = parsed.get(k)
            if isinstance(v, list) and v:
                # Primary host shape: list of response dicts.
                if isinstance(v[0], dict):
                    reply = str(v[0].get("user_response") or "")
                else:
                    reply = str(v[0])
                break
            elif isinstance(v, dict) and v:
                # Legacy dict shape: {"q0": "..."} or {"q0": {...}}
                val = next(iter(v.values()))
                if isinstance(val, dict):
                    reply = str(val.get("user_response") or "")
                else:
                    reply = str(val)
                break
            elif isinstance(v, str) and v:
                reply = v
                break
    elif isinstance(parsed, list) and parsed:
        reply = str(parsed[0])
    elif isinstance(parsed, str):
        reply = parsed
    return q, (reply or "").strip()


def _record_clarify_evidence(args: Any, result: Any, status: Any,
                             session_id: Optional[str] = None) -> None:
    """Record a host clarify call as transcript evidence for the code_map
    target it asked about (Q3, owner policy: 模型推 clarify, transcript 兜底).

    C-0 + C-lite:
      - C-0: question text now extracted via _extract_clarify_qr so the
        host's real plural ``questions`` schema (not the legacy single
        ``question`` dict) is honored. The legacy shape still works.
      - C-lite: ``session_id`` (host-kwargs ``session_id``, passed through
        by _post_tool_call) is stamped into the evidence line so the gate
        can reject evidence minted in a DIFFERENT session than the one
        calling code_map_check_root(yes=true). The caller (gate) is the
        authoritative session boundary; a hand-written evidence line with
        a stale/missing session_id fails the gate check.
      result: the user's reply (a choice label, free text, or a JSON-encoded
            structure from the host renderer; see _extract_clarify_qr).

    Only successful clarify calls are recorded (a cancelled/errored one is
    NOT evidence the user consented). The target is extracted from the
    question via the confirmed-target registry: the question must mention
    a path that lies under a confirmed target — that is the join key
    between "this clarify" and "that later code_map_check_root(yes=true)".
    Fail-soft: any parse hiccup -> no record, never breaks the tool call.
    """
    if status is not None and str(status).lower() not in ("ok", "success", "completed"):
        return
    try:
        q, reply = _extract_clarify_qr(args, result)
        if not q:
            return
        if not reply:
            return
        # Join key: which confirmed target does the question ask about?
        # Rule (shared with the gate-side lookup, RISK #6 fallback): the
        # question may mention the target verbatim OR by a trailing sub-path
        # of >= 2 consecutive path segments (e.g. 'plugins/code_map' for a
        # deep target) — that is enough for the join; a bare single segment
        # is not. Longest-matching target still wins.
        best: Optional[str] = None
        from .query import target_match, _extract_path_mentions
        confirmed = rootreg.confirmed_targets()
        for target in confirmed:
            if target and target_match(q, target):
                if best is None or len(target) > len(best):
                    best = target
        if best is not None:
            from .query import record_clarify_evidence
            record_clarify_evidence(best, reply, session_id=session_id)
            return
        # New-path fallback (RISK #6 follow-up, found in the 2026-10-11
        # knowledge-base-search live test): the model is asking to index a
        # path that is NOT confirmed yet — Q3's core case, which by
        # definition cannot be in confirmed_targets(). Extract the
        # path-shaped mentions from the question; a mention that spells out
        # the target VERBATIM (full, '/'-segregated) in the question is
        # still an explicit reference to THAT path, so we record evidence
        # for it — but ONLY that mention, longest-first, and ONLY when it
        # is a real absolute-looking path (>= 2 segments, starts with '/'
        # in the question text) — a bare relative 'x/y' fragment with no
        # leading '/' in the question text is too collision-prone to mint
        # evidence for a never-seen path. This deliberately does NOT reuse
        # the trailing-sub-path fallback (target_match(allow_partial=False)):
        # it is safe for an ALREADY-confirmed target (user already
        # consented to that exact path) but must not be the trigger for
        # minting evidence for a brand-new path the user has never been
        # asked about — that would let a casual 'plugins/foo' mention in
        # any unrelated question auto-approve an index of /…/plugins/foo.
        from .query import _extract_path_mentions
        for cand in _extract_path_mentions(q):
            # cand is already an absolute, >=2-segment path mention from the
            # question (the extractor returns only those, longest-first).
            # A verbatim match back into the question confirms this exact
            # string is what was asked about.
            if cand in q:
                best = os.path.abspath(cand)
                break
        # RISK #3 fix (reviewer deleg_1993fe7a, 2026-10-12): the host
        # clarify tool supports OPEN-ENDED questions (no choices, and a
        # question text that names NO path at all — e.g. "要建
        # code_map 索引吗？"). If such a question's reply TYPES an
        # absolute path, that typed path must mint evidence even though
        # the question arm found no `best` above — previously this whole
        # block was unreachable (early `return` when `best is None`),
        # silently dropping the evidence.
        from .query import _is_affirmative, record_clarify_evidence
        if best is not None:
            record_clarify_evidence(best, reply, session_id=session_id)
        # 'Other' free-text correction channel (owner's 2026-10-12 ruling):
        # the host clarify UI auto-appends an 'Other' free-text row; if the
        # user's reply TYPES an absolute path that the QUESTION itself did
        # not mention, that typed path is the user's own explicit consent
        # to index THAT path — record it as an additional evidence line
        # (same reply, same ts). Only when the reply is affirmative
        # (a refusal that mentions a path is not consent to that path, so
        # this arm is skipped when the reply is negative — see
        # test_hook_user_negative_reply_mints_no_evidence_even_with_path).
        if _is_affirmative(reply):
            # RISK #2 fix (reviewer deleg_1993fe7a, 2026-10-12): dedupe
            # on the NORMALIZED (abspath) form, not raw text — the
            # question may spell the same path in a non-canonical form
            # ('/a/../a/x', trailing spaces, double slashes) that the
            # reply spells canonically; record_clarify_evidence itself
            # stores os.path.abspath(target), so the comparison has to
            # happen at the same normalization level or a redundant,
            # duplicate evidence line gets appended for the same target.
            q_mentions_norm = {os.path.abspath(c) for c in _extract_path_mentions(q)}
            for cand in _extract_path_mentions(reply):
                if os.path.abspath(cand) not in q_mentions_norm:
                    record_clarify_evidence(os.path.abspath(cand), reply, session_id=session_id)
    except Exception as exc:
        _log.debug("code_map clarify evidence record fail-soft: %s", exc)


# ── turn-boundary un-indexed-project clarify (owner 2026-10-13 design) ────
# The per-read footer's clarify-trigger role moved to a TURN-BOUNDARY face:
#   write:  post_tool_call (read tool) -> _log_unindexed_read  (records the
#           read's enclosing DIRECTORY, not the file)
#   read:   pre_llm_call (host hook)  -> on_pre_llm_call        (delivers the
#           three-option clarify instruction, once, at the turn start)
#   settle: post_tool_call (clarify)  -> _settle_clarify_answer (要 / 不要 /
#           不再问 each clear the ledger in a distinct way)
# Ledger file: data_dir()/_unindexed_read_ledger — one line per pending
# project:  <project_dir>\t<session_id>
# Session-scoped: a session's rows are read only by that session's
# pre_llm_call face, so rows never leak across sessions (owner's P2
# simplification: the 3-choice closes cross-session persistence — "不再问"
# = row deleted = never asked again, no extra state needed).

def _unindexed_read_ledger() -> "Path":
    from .config import data_dir
    return data_dir() / "_unindexed_read_ledger"


def _ledger_read() -> list:
    """Return the ledger's non-empty lines, fail-soft to []."""
    p = _unindexed_read_ledger()
    if not p.exists():
        return []
    try:
        return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []


def _ledger_write(lines: list) -> None:
    """Rewrite the ledger with `lines` (already canonical order), fail-soft.

    An empty `lines` deletes the file (no 0-byte ledger residue left
    behind, mirroring rootreg's own fail-soft style).
    """
    p = _unindexed_read_ledger()
    try:
        if lines:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
        elif p.exists():
            p.unlink()
    except OSError:
        pass


def _project_boundary(target: str) -> Optional[str]:
    """The footer's honest project boundary for `target` (a file path):
    detect_root's marker result when it actually encloses the file, else
    the file's immediate parent dir — the SAME rule the retired footer used,
    kept in one place so the ledger key and the question text always name
    the same boundary. Returns None when no meaningful boundary exists."""
    from .rootdetect import detect_root
    proj_root = detect_root(target)
    if proj_root is None or not (target == proj_root or target.startswith(proj_root + os.sep)):
        proj_root = os.path.dirname(target)
    if proj_root in ("", os.sep):
        return None
    return proj_root


def _log_unindexed_read(tool_name: str, args: Any, session_id: Optional[str]) -> None:
    """post_tool_call read side: record the file's ENCLOSEING DIRECTORY for
    a source-file read that landed outside any confirmed target.

    Judgment key is the DIRECTORY, not the file (owner's 2026-10-13
    correction): N reads under one project coalesce to ONE ledger line, so
    the pending set is O(projects touched) not O(files touched).

    Only source files (INDEXED_EXTENSIONS, non-binary) whose boundary has
    neither a confirmed target nor a db record get logged — the same
    two checks the retired footer ran, so an already-indexed/confirmed
    project never produces a pending row.
    Fail-soft: any hiccup -> no ledger line, never breaks the read.
    """
    try:
        target = (args or {}).get("path") or (args or {}).get("pattern") or ""
        if not target:
            return
        target = os.path.abspath(str(target))
        from .config import INDEXED_EXTENSIONS
        ext = os.path.splitext(target)[1].lower()
        if ext not in INDEXED_EXTENSIONS or _is_binary(target):
            return
        confirmed = rootreg.confirmed_targets()
        if _find_confirmed_root(target, set(confirmed)) is not None:
            return  # under a confirmed target -> the attach path, not pending
        proj = _project_boundary(target)
        if proj is None:
            return
        if rootreg.is_confirmed_root(proj):
            return
        from .config import root_db_path
        if root_db_path(proj).exists():
            return  # was indexed before -> refresh case, not "never indexed"
        if not session_id:
            return  # no session to scope the row to -> skip, never leak
        line = f"{proj}\t{session_id}"
        lines = _ledger_read()
        if line not in lines:
            lines.append(line)
            _ledger_write(lines)
    except Exception as exc:
        _log.debug("code_map unindexed-read ledger log fail-soft: %s", exc)


def _pending_projects(session_id: str) -> list:
    """This session's pending project dirs, re-filtered at read time:
    rows whose project is NOW confirmed or already has a db record are
    dropped (the user may have confirmed between the read and this turn —
    the row auto-falls out, no stale question). Returns a sorted,
    de-duplicated list of boundary dirs."""
    out: list = []
    for line in _ledger_read():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        proj, sid = parts
        if sid != session_id:
            continue
        if proj in out:
            continue
        if rootreg.is_confirmed_root(proj):
            continue
        try:
            from .config import root_db_path
            if root_db_path(proj).exists():
                continue
        except Exception:
            continue
        out.append(proj)
    return sorted(out)


def _clear_project_rows(session_id: str, project_dirs) -> None:
    """Remove this session's ledger rows for `project_dirs` (exact, per-dir
    settle used by '要'/'不再问')."""
    drop = {os.path.abspath(d) for d in project_dirs}
    kept = []
    for line in _ledger_read():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1] == session_id and parts[0] in drop:
            continue
        kept.append(line)
    _ledger_write(kept)


def _clarify_named_project(args: Any) -> Optional[str]:
    """Which project dir does this clarify call ask about? Extracted from
    the question text's path mentions (absolute, >= 2 segments, longest
    first) — the same extractor the Q3 evidence gate uses, so '要 index
    /root/.hermes/plugins/emo 吗？' names that dir precisely. The 3 fixed
    options (要/不要/不再问) carry no path; the path always comes from the
    question the model wrote (the system-prompt policy instructs the model
    to put the full target path in the clarify question, and
    on_pre_llm_call's notice text hands it over verbatim)."""
    from .query import _extract_path_mentions
    q, _reply = _extract_clarify_qr(args, None)
    if not q:
        return None
    mentions = _extract_path_mentions(q)
    if not mentions:
        return None
    # Longest mention = the most specific target the question names.
    best = max(mentions, key=len)
    return os.path.abspath(best)


def _clarify_reply_text(result: Any) -> str:
    """The user's reply from a host clarify result payload (the first
    question's user_response, whether single- or multi-question shape)."""
    if not result:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        responses = result.get("responses")
        if isinstance(responses, list) and responses:
            first = responses[0]
            if isinstance(first, dict):
                return str(first.get("user_response") or "")
            return str(first)
        # Legacy single-question shape.
        return str(result.get("user_response") or "")
    return ""


def _settle_clarify_answer(args: Any, result: Any, status: Any,
                           session_id: Optional[str]) -> None:
    """Turn-boundary ledger settle on a host clarify answer (owner's 2026-10-13
    three-option model, exact semantics: 要 / 不要 / 不再问):
      - '要'     -> the project is being indexed now; clear ONLY that
                   project's row (it becomes a confirmed target next turn;
                   the explicit clear is belt-and-suspenders over the
                   auto-drop).
      - '不要'   -> the user is not indexing RIGHT NOW, but the question
                   stands: clear nothing, the row stays, the SAME project
                   gets asked again on its next read.
      - '不再问' -> clear the named project's row; that project is silently
                   dropped permanently (row removed = never asked again).
    Only SUCCESSFUL clarify calls settle (a cancelled/errored call is not
    an answer — the next turn asks again, same as the evidence gate).
    Fail-soft: a parse hiccup leaves the ledger untouched.
    """
    if status is not None and str(status).lower() not in ("ok", "success", "completed"):
        return
    if not session_id:
        return
    reply = _clarify_reply_text(result).strip()
    try:
        if reply.startswith("不再问"):
            proj = _clarify_named_project(args)
            if proj:
                _clear_project_rows(session_id, [proj])
            return
        # '不要' (and every non-'要' answer) settles nothing: the pending
        # rows stay, the same project is asked again on its next read.
        if not (reply == "要" or (reply.startswith("要") and len(reply) <= 2)):
            return
        proj = _clarify_named_project(args)
        if proj:
            _clear_project_rows(session_id, [proj])
    except Exception as exc:
        _log.debug("code_map clarify-answer settle fail-soft: %s", exc)


def on_pre_llm_call(session_id: str = "", user_message: str = "",
                    conversation_history: Optional[list] = None, model: str = "",
                    platform: str = "", **_: Any) -> Optional[Dict[str, str]]:
    """pre_llm_call hook dispatcher (host-facing): the turn-boundary face
    that delivers the "你摸了未索引项目 X，请调 clarify 问用户是否建索引"
    question ONCE per turn, listing this session's still-pending project
    dirs with the owner's three fixed clarify options (要 / 不要 / 不再问).

    Returns None when nothing is pending (no ledger rows for this session,
    or every row auto-fell out because the project is now confirmed / has
    a db record) — a clean turn costs the host nothing (one small file
    read). The returned `context` rides the host's pre_llm_call injection
    (appended to the user message, same shape filemem's auto-retrieve
    returns). Fail-soft: any exception -> None, never breaks the LLM call.
    """
    try:
        if not session_id:
            return None
        projects = _pending_projects(session_id)
        if not projects:
            return None
        lines = [f"[code_map] 上一轮你读了未索引项目的源码，请调 clarify 问用户"
                 f"是否为它建代码地图（三个选项：要 / 不要 / 不再问）："]
        for proj in projects:
            lines.append(f"  - {proj}")
        lines.append("（语义：要 -> 调 code_map_check_root(root=<boundary>, "
                     "path=<rel>, yes=True) 建索引；不要 -> 暂不建，"
                     "下次读到该项目仍会再问；不再问 -> 永不再就该项目询问。）")
        return {"context": "\n".join(lines)}
    except Exception as exc:
        _log.debug("code_map pre_llm_call notice fail-soft: %s", exc)
        return None


def _post_tool_call(tool_name: str = "", args: Any = None,
                    result: Any = None, **_: Any) -> Optional[str]:
    """post_tool_call dispatcher.

    The host invokes post_tool_call with kwargs: tool_name, args, result,
    duration_ms, status ("ok"|"error"|None), error_type, error_message,
    middleware_trace + the _CallIds kwargs (see model_tools._emit_post_tool_call_hook).
    `status` is the AUTHORITATIVE success signal (the host derives it from the
    result), so we gate the reindex on it when present and fall back to
    inspecting the result dict only when status is None (legacy / unit-test
    callers that don't pass status).
    """
    try:
        # Read-side ledger: a read_file/search_files landing OUTSIDE any
        # confirmed target logs the file's enclosing DIRECTORY (project
        # boundary) into the session-scoped ledger. The turn-boundary
        # pre_llm_call face (on_pre_llm_call) is what delivers the
        # "ask the user whether to build a code map" question — the
        # per-read transform_tool_result footer is retired for this role.
        if tool_name in _READ_TOOLS:
            _log_unindexed_read(tool_name, args, _.get("session_id"))
        # Q3 transcript 兜底：宿主 clarify 工具的一次调用 = "用户被问到且
        # 已应答"的权威 transcript 证据。args 里携带 code_map 目标
        # （策略文本要求模型把目标写进 clarify 的问题里），result 里是
        # 用户的应答文本。两者齐全且调用成功才落 evidence log。
        # Also: the 3-option clarify (要/不要/不再问) answer clears the
        # un-indexed-read ledger per the owner's 2026-10-13 ruling.
        if tool_name == "clarify":
            _record_clarify_evidence(args, result, _.get("status"),
                                    session_id=_.get("session_id"))
            _settle_clarify_answer(args, result, _.get("status"),
                                   session_id=_.get("session_id"))
            return None
        if tool_name not in _WRITE_TOOLS:
            return None
        # Success gate (件 1): only reindex a write that actually SUCCEEDED.
        # The old transform_tool_result path reindexed error results too —
        # that is the bug this replaces.
        status = _.get("status")
        if status is not None:
            if str(status).lower() in ("ok", "success", "completed"):
                pass  # proceed to reindex
            else:
                return None  # error/failed -> skip (index keeps its prior state)
        else:
            # Legacy / unit-test caller: derive from the result dict.
            if isinstance(result, dict):
                if result.get("error") or result.get("status") in ("error", "failed"):
                    return None
        return _maybe_reindex(tool_name, args)
    except Exception as exc:
        _log.debug("code_map post_tool_call fail-soft: %s", exc)
        return None


def _maybe_reindex(tool_name: str, args: Any) -> Optional[str]:
    """Synchronous single-file reindex under a confirmed target.

    Only acts on write_file/patch. Skips anything not under a confirmed
    target (gate ⑨). Debounces bursts (HOOK_DEBOUNCE_S). Runs the re-parse
    + upsert SYNCHRONOUSLY (ms-scale; background threads raced the commit
    and left the index stale — the 12-min-lag incident). Marks the target
    dirty so the read side knows a full rebuild may be due.
    """
    if tool_name not in _WRITE_TOOLS:
        return None
    # (verified against the host tool schemas). The `.get("target")`
    # fallback covers no current host tool and is removed (reviewer F3).
    file_path = (args or {}).get("path")
    if not file_path:
        return None
    file_path = str(file_path)

    confirmed = _confirmed_roots()
    root = _find_confirmed_root(file_path, confirmed)
    if root is None:
        return None

    with _debounce_lock:
        now = time.time()
        last = _last_schedule.get(root, 0.0)
        if now - last < HOOK_DEBOUNCE_S:
            return None  # already reindexed for this burst
        _last_schedule[root] = now

    _reindex_single_file_sync(root, file_path)
    rootreg.mark_dirty(root)
    return None


def _reindex_single_file_sync(root: str, file_path: str) -> None:
    """Parse one file and upsert it, synchronously (correctness-first).

    Fail-soft: parse errors / missing grammar -> the file is skipped (the
    index keeps its prior state), never a crash, never blocks the tool.

    Concurrency (reviewer d-1/d-2): the upsert section holds BOTH the
    in-process `_heal_lock` (so a same-process full-build heal cannot
    interleave its diff snapshot with our upsert) AND the cross-process
    `write_lock` (fcntl flock on the shared data dir's _proc.lock). The
    flock is ADVISORY here (nowait): it is acquired when free so two
    same-root writers serialize, but if another process holds it we
    still proceed — the upsert is a single-file, idempotent WAL write
    and must never block a host worker (reviewer F1 budget). The
    target is stamped dirty either way, so the next read-side heal
    folds any missed change into a full rebuild.
    """
    from .db import init_db, insert_symbols, upsert_file
    from .fingerprint import file_fingerprint, invalidate_cache
    from .lockfile import write_lock
    from .parser import parse_file
    from .registry import EXT_TO_LANG
    from pathlib import Path

    try:
        p = Path(file_path)
        ext = p.suffix.lower()
        lang = EXT_TO_LANG.get(ext, "unknown")
        if lang == "unknown":
            return  # not an indexable source file
        with _heal_lock:
            with write_lock(nowait=True, what="single-file reindex"):
                conn = init_db(root)
                try:
                    rows = parse_file(file_path) or []
                    mtime, size = file_fingerprint(p)
                    upsert_file(conn, file_path, mtime, size, lang, "hook")
                    insert_symbols(conn, root, file_path, rows, commit=True)
                    invalidate_cache(root)
                finally:
                    conn.close()
    except Exception as exc:
        _log.debug("code_map single-file reindex fail: %s", exc)


# ── read side: auto-attach symbol outline (件 2) + dirty heal (件 3) ────────

def _maybe_nudge(tool_name: str, args: Any, result: Any = None) -> Optional[str]:
    """Read-side auto-attach.

    If the read target is a source file (INDEXED_EXTENSIONS) under a
    confirmed target, query the map for that file's own symbols (top 30) +
    its import edges (top 8) and return an attachment string that the host
    prepends to the tool result. If the target is dirty, run a synchronous
    full rebuild first (件 3 self-heal), then clear the dirty stamp.

    Falls back to the plain nudge text when the query path is unavailable
    (corrupt db / missing venv) — the read result is never broken.
    """
    if tool_name not in _READ_TOOLS:
        return None
    target = (args or {}).get("path") or (args or {}).get("pattern") or ""
    if not target:
        return None  # no target to test against the confirmed registry
    target = os.path.abspath(str(target))

    # Gate ①: must land UNDER a confirmed target. Anything outside (e.g.
    # /etc/hostname) -> None, no nudge, no attach. `read_file` passes a
    # concrete `path`; `search_files` passes a `pattern` (a regex/glob, not
    # an absolute file path) — `os.path.abspath` on a bare pattern lands in
    # the CWD, so it never matches a confirmed target and falls through to
    # the None path (a search pattern carries no specific file to attach
    # symbols for; the host still gets the directory-level nudge when it
    # later READS a file in that target).
    confirmed = rootreg.confirmed_targets()
    enclosing = _find_confirmed_root(target, set(confirmed))
    if enclosing is None:
        # Not under any confirmed target. The clarify-trigger role moved
        # to the turn-boundary pre_llm_call face (owner's 2026-10-13
        # ruling: the per-read mid-turn footer "抢跑" and is bypassed by a
        # model switch to terminal/execute_code). This read-side path now
        # contributes NOTHING to the tool result for an out-of-scope read
        # — the ledger write (post_tool_call side) is what captures the
        # "model read an un-indexed project" fact for the turn-boundary
        # question.
        return None

    # Must be a source file (extension in INDEXED_EXTENSIONS) — NOT .md/.json
    # and not a binary. Non-source paths under a confirmed target still get
    # the plain directory nudge (useful "you're in an indexed dir" signal).
    from .config import INDEXED_EXTENSIONS
    ext = os.path.splitext(target)[1].lower()
    if ext in INDEXED_EXTENSIONS and not _is_binary(target):
        root = enclosing
        # 件 3: if dirty, heal before attaching (sync full rebuild, ms-scale
        # at ~100-file scale; for larger targets this is the one case where
        # the read path does a full walk — still bounded, and it is the
        # price of "never serve a stale map").
        if rootreg.is_dirty(root):
            _heal_dirty(root)
        attached = _attach_symbols(root, target)
        if attached:
            return attached
        return _plain_nudge(root)  # map query failed -> plain nudge
    return _plain_nudge(enclosing)  # non-source path under a confirmed target


def _plain_nudge(target_or_root: str) -> Optional[str]:
    """The legacy one-line nudge, kept as the fail-soft fallback."""
    confirmed = rootreg.confirmed_targets()
    if not confirmed:
        return None
    return (
        f"[code_map] {len(confirmed)} confirmed target(s) indexed. "
        f"For cross-file symbol lookup prefer code_map_query "
        f"(root=<confirmed target>, name=... or path=...) over whole-tree grep."
    )


def _is_binary(path: str) -> bool:
    """NUL-byte sniff on the first 512 bytes (cheap, no full read)."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
        return b"\x00" in head
    except OSError:
        return False  # unreadable -> treat as text (let the query fail-soft)


def _heal_dirty(root: str) -> None:
    """件 3 self-heal, review-hardened (reviewer F1/F2/d-2).

    Budgets the synchronous full rebuild so a hot-path read can NEVER
    wedge the host's bounded hook worker (transform_tool_result is in
    _HOOK_TIMEOUT_BOUNDED_HOOKS, 30s default, fail-open skip + 60s
    suppression):

      - Size guard: only attempt the sync rebuild when the target's current
        index holds <= READ_HEAL_MAX_FILES files (the ms-scale regime the
        original design assumed). Above the threshold, a full walk+parse
        can take minutes (30k files ~29s cold); instead we attach from the
        (stale-but-present) db as-is and CLEAR the dirty stamp so reads
        stop re-triggering full walks. An explicit code_map_refresh remains
        the only way to catch up a large target.
      - Give-up guard: after READ_HEAL_MAX_ATTEMPTS consecutive FAILED heals
        (corrupt db, lock, disk error), stop retrying — leave the stamp
        for an explicit refresh, and reads fall back to the plain nudge
        without attempting another full rebuild.
      - In-process re-entrancy guard: a module-level lock so two concurrent
        reads of the same target cannot stack two full-walk workers.
      - Cross-process guard (reviewer d-2): the full build also holds the
        shared flock from lockfile.py (blocking, < the 30s host budget) so
        two host/profile processes never run two full walks over the same
        db at once.
      - Fail-soft: any exception is swallowed; the current read degrades to
        the plain nudge. The dirty stamp is only cleared on a SUCCESSFUL
        rebuild (or by the size/give-up guards above).
    """
    from .state import _safe_run_build
    from .lockfile import write_lock

    with _heal_lock:
        # ① size guard — bound the hot-path cost.
        indexed = _count_indexed_files(root)
        if indexed > READ_HEAL_MAX_FILES:
            # Too big to heal on the hot path: attach from the stale db,
            # stop hammering full walks, keep the target usable.
            rootreg.clear_dirty(root)
            return
        # ② give-up guard — stop retrying a persistently failing rebuild.
        key = os.path.abspath(root)
        if rootreg.heal_attempts(key) >= READ_HEAL_MAX_ATTEMPTS:
            return  # stamp stays; reads stop trying (plain nudge below)
        # ③ run the sync rebuild under the cross-process lock.
        # `_safe_run_build` makes the failure path explicit (logs a WARNING,
        # never escapes the thread) and hands the give-up counter back via
        # `on_fail`; it returns True iff the build actually succeeded, so
        # the dirty stamp is cleared ONLY on success — a failed build leaves
        # it set (with the counter bumped by on_fail) so the next read
        # retries until the give-up cap.
        with write_lock(nowait=False, what="heal-build"):
            ok = _safe_run_build(root,
                                 on_fail=lambda _r: rootreg.note_heal_failure(key))
        if ok:
            rootreg.clear_dirty(root)
            rootreg.reset_heal_attempts(key)


def _count_indexed_files(root: str) -> int:
    """Cheap sqlite COUNT of the target's current index (fail-soft -> 0).
    Bounded read (one COUNT query, no walk) — safe on the hot path."""
    from .config import root_db_path
    import sqlite3
    db = root_db_path(root)
    if not db.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return 0


def _attach_symbols(root: str, file_path: str) -> Optional[str]:
    """Query the map for this file's own symbols + import edges; return the
    attachment text, or None when the query path is unavailable.

    `query_symbols` matches the symbols table on the ABSOLUTE path of the
    read target exactly (b-1 fix: the old LIKE '%'+rel also matched every
    same-basename file in sibling subpackages)."""
    from .db import init_db, query_symbols
    from .config import root_db_path

    db_path = root_db_path(root)
    if not db_path.exists():
        return None
    try:
        conn = init_db(root)
        try:
            abs_path = os.path.abspath(file_path)
            # ① this file's own symbols (top READ_SELF_SYMBOLS)
            own_rows = query_symbols(conn, root, path=abs_path,
                                     limit=READ_SELF_SYMBOLS)
            # ② cross-file import edges FROM this file (kind='import',
            #    restricted to this file's rows)
            import_rows = query_symbols(conn, root, path=abs_path,
                                       kind="import",
                                       limit=READ_IMPORT_EDGES)
        finally:
            conn.close()
    except Exception as exc:
        _log.debug("code_map attach query failed: %s", exc)
        return None

    rel = os.path.relpath(file_path, root)
    own = [(r[2], r[3], r[4], r[5]) for r in own_rows]  # name,kind,line,sig
    imps = [(r[2], r[5]) for r in import_rows]           # name, sig
    if not own and not imps:
        return None

    lines = [f"[code_map] {rel}: {len(own)} symbol(s) in this file, "
             f"{len(imps)} import edge(s) attached (top-N, byte-capped):"]
    for name, kind, line, sig in own[:READ_SELF_SYMBOLS]:
        sig_s = (sig or "").strip()
        suffix = f"  {sig_s}" if sig_s and len(sig_s) <= 60 else ""
        lines.append(f"  {kind:<9} L{line:<4} {name}{suffix}")
    if imps:
        lines.append(f"  imports: " + ", ".join(n for n, _ in imps[:READ_IMPORT_EDGES]))
    # Byte-capped (reviewer F4 fix): slice on the ENCODED form, not the str —
    # the old char-slice `out[:READ_SYMBOL_MAX_BYTES]` could land a
    # multi-byte CJK run at the boundary and the appended marker pushed the
    # total PAST the cap. Encoding first makes the cap exact (<= cap + the
    # marker) and errors='ignore' guarantees a clean UTF-8 boundary.
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    if len(raw) > READ_SYMBOL_MAX_BYTES:
        marker = "\n  …(truncated to byte cap)".encode("utf-8")
        budget = max(READ_SYMBOL_MAX_BYTES - len(marker), 0)
        out = raw[:budget].decode("utf-8", errors="ignore") + marker.decode("utf-8")
    else:
        out = raw.decode("utf-8", errors="ignore")
    return out


# ── host-facing dispatcher ──────────────────────────────────────────────────

def on_result(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    **_: Any,
) -> Optional[str]:
    """transform_tool_result hook dispatcher.

    - write_file/patch -> now handled by post_tool_call (件 1); kept here as
      a no-op for the case the host still routes writes through this hook
      (the legacy transform_tool_result registration stays until the host
      fully adopts post_tool_call).
    - read_file/search_files -> _maybe_nudge (件 2 auto-attach + 件 3 heal).

    Fail-soft: any exception -> None (gate 9).
    """
    try:
        if tool_name in _WRITE_TOOLS:
            return None  # write side moved to post_tool_call; no-op here
        return _maybe_nudge(tool_name, args, result)
    except Exception as exc:
        _log.debug("code_map hook fail-soft: %s", exc)
        return None


def _confirmed_roots() -> set:
    """Read the set of confirmed targets from the registry (via rootreg)."""
    return set(rootreg.confirmed_targets())


def _find_confirmed_root(file_path: str, confirmed: set) -> Optional[str]:
    """Return the confirmed target that `file_path` falls under (nearest)."""
    abspath = os.path.abspath(file_path)
    best = None
    for root in confirmed:
        root_abs = os.path.abspath(root)
        if abspath.startswith(root_abs + os.sep) or abspath == root_abs:
            if best is None or len(root_abs) > len(best):
                best = root_abs
    return best
