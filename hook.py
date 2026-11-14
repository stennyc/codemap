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
                    separate code_map_query round-trip. P0 PRESERVATION
                    (owner ruling 2026-10-15): the host's transform REPLACES
                    the result with the first string a hook returns, so the
                    attach block is always prepended to a verbatim copy of
                    the original result body (_with_body) — the model never
                    loses the file content to the attach. If the target is
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
import fcntl
import re
import time
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional

from .config import (CROSS_FOLDER_LINE_MAX_BYTES, HOOK_DEBOUNCE_S,
                    READ_HEAL_MAX_ATTEMPTS, READ_HEAL_MAX_FILES,
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
_TERMINAL_TOOL = "terminal"


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


# Re-entrancy guard: one lock so two concurrent _ledger_rmw calls in the
# SAME process (host's bounded post_tool_call worker threads, or pre_llm_call
# + post_tool_call racing) cannot stack up. flock handles cross-process
# (hardlink-shared data dir); this lock handles the same-process, cross-thread
# case, which fcntl.flock on a per-thread fd does NOT serialize against
# (each thread opens its own fd and gets its own lock handle).
# Guarded: see _ledger_write_lock below.
_ledger_thread_lock = threading.Lock()


@contextmanager
def _ledger_write_lock(nowait: bool = True):
    """Ledger's advisory cross-process write lock, on ITS OWN lock file
    (data/_ledger_proc.lock — deliberately NOT the db's _proc.lock): the
    single-file reindex holds the db lock for the duration of its sqlite
    write; serializing ledger RMWs on that same lock would push read-side
    workers into a 20-30s contention window — the exact 12-min-lag
    incident shape the single-file reindex replaced. A lost update here
    degrades to 'asked one fewer time' (fail-soft), so nowait + fail-open
    is the right posture: never block a host worker.

    Same fcntl.flock mechanism as the db lock, so cross-process
    mutual exclusion works the way owner's hardlink invariant demands
    (one physical lock file shared by all host/profile processes — the
    ledger lock file lives in the same hardlinked data dir).
    """
    import fcntl
    import time
    from .config import data_dir
    path = data_dir() / "_ledger_proc.lock"
    try:
        path.touch(exist_ok=True)
        fd = open(path, "w")
    except OSError:
        yield  # lock file unobtainable -> run unlocked (fail-open)
        return
    acquired = False
    try:
        if nowait:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass  # held -> advisory: run the body anyway
        else:
            deadline = time.monotonic() + 25.0
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break  # deadline -> proceed without it
                    time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            fd.close()
        except OSError:
            pass


def _ledger_read() -> list:
    """Return the ledger's non-empty lines, fail-soft to []."""
    p = _unindexed_read_ledger()
    if not p.exists():
        return []
    try:
        return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []


def _ledger_rmw(mutate) -> None:
    """Read-mutate-write the ledger under its own advisory cross-process
    flock (`_ledger_write_lock`, on data/_ledger_proc.lock — a SEPARATE
    lock file from the db's _proc.lock, so a 20-30s single-file-reindex
    critical section never stalls the read-side ledger RMW): the current
    lines go through `mutate(current) -> new_lines` and the result is
    written back, all inside one critical section, so two host/profile
    processes (post_tool_call runs on a bounded worker thread; the data
    dir is hardlinked cross-profile) cannot both read the same file,
    append their own row, and overwrite each other — a last-writer-wins
    lost update that would silently drop pending rows (reviewer round 1
    measured ~99.9% row loss in an 8000-iteration 4-thread race; a
    dropped row = the project is asked one fewer time, never data loss).

    The lock is nowait + fail-open (advisory, same posture as 件 1's
    single-file reindex lock): when the flock is held we still run the
    body, so a contested RMW degrades to the pre-lock fail-soft behaviour
    instead of ever blocking a host worker (the 30s bounded-hook budget
    applies). `mutate` returning None means "unchanged" (no write, no
    unlink.

    TWO-LEVEL locking (reviewer round 2, R2-1): the flock above is
    CROSS-PROCESS only — each host/profile process opens its own fd on
    the shared lock file, so two bounded worker threads inside the SAME
    host process do NOT serialize against each other (flock on distinct
    fds = distinct lock handles, both acquire). A process-local
    threading.Lock closes that hole; the two locks nest (thread first,
    then flock) so a cross-process + cross-thread race is still safe.
    """
    with _ledger_thread_lock:
        with _ledger_write_lock(nowait=True):
            new_lines = mutate(_ledger_read())
            if new_lines is None:
                return
            _ledger_write(new_lines)


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
    """The honest project boundary for `target` (a file path):
    detect_root's marker result when it actually encloses the file, else
    the file's immediate parent dir — kept in one place so the ledger key
    and the question text always name the same boundary. Returns None
    when no meaningful boundary exists.

    Special case (reviewer round 1): detect_root climbs to $HOME as its
    last-resort cap, so a marker-less project UNDER $HOME would otherwise
    get $HOME itself as the "project" — and the ledger/clarify question
    would then offer to index the whole home tree (the 382k-file
    incident's shape, and exactly what gate ⑩ refuses as a confirm root).
    $HOME is never a project boundary: downgrade to the file's parent dir.
    """
    from .rootdetect import detect_root
    proj_root = detect_root(target)
    if proj_root is None or not (target == proj_root or target.startswith(proj_root + os.sep)):
        proj_root = os.path.dirname(target)
    home = os.path.expanduser("~")
    if proj_root == home:
        # /root/x/y.py with no marker anywhere -> the project is /root/x,
        # NOT /root (the home tree is not indexable).
        proj_root = os.path.dirname(target)
    # R2-4 residual: a file DIRECTLY under $HOME ($HOME/y.py) downgrades to
    # $HOME itself, which is not a meaningful project boundary — no
    # sensible project sits at the home root. Return None (never log a
    # $HOME row).
    if proj_root == home or proj_root in ("", os.sep):
        return None
    return proj_root


def _log_unindexed_read(tool_name: str, args: Any, session_id: Optional[str]) -> None:
    """post_tool_call read side: record the file's ENCLOSEING DIRECTORY for
    a read that landed outside any confirmed target.

    Judgment key is the DIRECTORY, not the file (owner's 2026-10-13
    correction): N reads under one project coalesce to ONE ledger line, so
    the pending set is O(projects touched) not O(files touched).

    Directory-triggered, NOT source-file-triggered (owner 2026-10-18,
    defect 2): ANY file read under an un-indexed project's directory logs
    the project row — the legacy INDEXED_EXTENSIONS filter made projects
    whose read files were .md/.json/.lock etc. invisible to the
    turn-boundary clarify offer (the project was never proposed for
    indexing). A binary read is still skipped (it is not "source" we would
    ever index, and a NUL-sniff on the first 512 bytes is cheap).

    Two guards keep this honest:
      - the read must NOT already land under a confirmed target (the attach
        path, not the pending path);
      - the project boundary must NOT itself be a real system tree — the
        hardcoded ("/usr", "/etc", "/opt") guard (plus bare "/") is the
        gate-⑩ incident shape; those trees are never offered for indexing.
        /root and /home are deliberately NOT guarded: owner's projects live
        under /root and ARE the indexable case, and a real project under
        them resolves to its own marker dir via detect_root, not to /root.
    Fail-soft: any hiccup -> no ledger line, never breaks the read.
    """
    try:
        target = str((args or {}).get("path") or (args or {}).get("pattern") or "")
        if not target:
            return
        # The search_files `pattern` is a regex/glob, not a path: a bare
        # relative pattern ('*.py') abspaths to the CWD and splitext would
        # log the CWD itself as a "project" (reviewer round 1). Only an
        # ABSOLUTE pattern names a real directory; a relative pattern is
        # too CWD-dependent to settle — read_file is the reliable trigger
        # (the attach path's search_files note at 件 2 says the same thing:
        # a pattern carries no specific file to settle against). This check
        # MUST run on the raw (pre-abspath) value: a relative input's
        # abspath is itself the noise we're trying to reject.
        if tool_name == "search_files" and not os.path.isabs(target):
            return
        target = os.path.abspath(target)
        # A binary read is not a project source we would index; skip it.
        if _is_binary(target):
            return
        confirmed = rootreg.confirmed_targets()
        if _find_confirmed_root(target, set(confirmed)) is not None:
            return  # under a confirmed target -> the attach path, not pending
        proj = _project_boundary(target)
        if proj is None:
            return
        # System-tree guard (owner 2026-10-18, defect 2): if the resolved
        # project boundary IS a real system tree we never index (/usr, /etc,
        # /opt — the gate-⑩ incident shape) OR the bare filesystem root,
        # skip the row. /root and /home are deliberately NOT guarded here:
        # they are user workspaces (owner's projects like /root/.hermes/
        # plugins/emo live under /root and ARE the indexable case), and a
        # real project under them resolves to ITS OWN marker dir via
        # detect_root, not to /root itself.
        proj_abs = os.path.abspath(proj)
        for prefix in ("/usr", "/etc", "/opt"):
            p = os.path.abspath(prefix)
            if proj_abs == p or proj_abs.startswith(p + os.sep):
                return
        if proj_abs == os.sep:
            return
        if rootreg.is_confirmed_root(proj):
            return
        from .config import root_db_path
        if root_db_path(proj).exists():
            return  # was indexed before -> refresh case, not "never indexed"
        if not session_id:
            return  # no session to scope the row to -> skip, never leak
        line = f"{proj}\t{session_id}"

        def _append(current):
            if line in current:
                return None  # unchanged, no write
            current.append(line)
            return current

        _ledger_rmw(_append)
    except Exception as exc:
        _log.debug("code_map unindexed-read ledger log fail-soft: %s", exc)


def _pending_projects(session_id: str) -> list:
    """This session's pending project dirs, re-filtered at read time:
    rows whose project is NOW a confirmed target, sits UNDER a confirmed
    target, or already has a db record are dropped (the user may have
    confirmed or a db may have appeared between the read and this turn —
    the row auto-falls out, no stale question). Owner 2026-10-19 added the
    under-a-confirmed-target case: that is the refresh scenario, not the
    "never indexed" one. Returns a sorted, de-duplicated list of boundary
    dirs."""
    confirmed = _confirmed_roots()
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
        if _find_confirmed_root(proj, confirmed) is not None:
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

    def _drop(current):
        kept = []
        for line in current:
            parts = line.split("\t")
            if len(parts) == 2 and parts[1] == session_id and parts[0] in drop:
                continue
            kept.append(line)
        # None = unchanged (skip the write, matching _ledger_rmw's
        # documented contract); a fresh list only when a row was dropped.
        return None if kept == current else kept

    _ledger_rmw(_drop)


# Settle-side session scope is passed EXPLICITLY as a parameter through
# _settle_clarify_answer -> _clarify_named_projects (reviewer round 2, R2-2:
# the old module-level dict was shared across concurrent worker threads,
# so thread B's session could bleed into thread A's boundary-upward
# mapping and clear the wrong session's rows). threading.local would also
# work; an explicit parameter is simpler and makes the dependency visible
# in the signature.


def _purge_obsolete_rows(tool_name: str, args: Any,
                        session_id: Optional[str]) -> None:
    """post_tool_call write side (owner 2026-10-19): drop this session's
    ledger rows for a project that this very read just proved obsolete —
    i.e. the read's boundary is a confirmed target, sits under a confirmed
    target, or has a db record (the refresh case, not "never indexed").

    The write-side guards in _log_unindexed_read skip LOGGING such rows,
    but they cannot ERASE a row that was logged BEFORE the target was
    confirmed / a db appeared (or by a session whose confirm/refresh
    happened later). Without this purge, on_pre_llm_call's
    _needs_index_build_question filter alone would still let a stale row
    survive across a confirm, and the '不再问' settle path would silently
    drop a refresh offer the user may want next time.
    Fail-soft: any hiccup -> no rows touched.
    """
    try:
        if not session_id:
            return
        target = str((args or {}).get("path") or (args or {}).get("pattern") or "")
        if not target:
            return
        if tool_name == "search_files" and not os.path.isabs(target):
            return
        proj = _project_boundary(target)
        if proj is None:
            return
        if _needs_index_build_question(proj):
            return  # genuinely pending -> keep the row, do not purge
        _clear_project_rows(session_id, [proj])
    except Exception as exc:
        _log.debug("code_map obsolete-row purge fail-soft: %s", exc)


def _clarify_named_projects(args: Any, session_id: str = "") -> list:
    """Every (project boundary) this clarify call asks about. Extracted from
    EACH question text's path mentions (absolute, >= 2 segments) — the same
    extractor the Q3 evidence gate uses. The 3 fixed options (要/不要/不再问)
    carry no path; the path always comes from the question(s) the model
    wrote (the system-prompt policy instructs the model to put the full
    target path in the clarify question, and on_pre_llm_call's notice text
    hands it over verbatim).

    Returns ALL distinct mentions across every question, not just the
    first (reviewer round 1: a model bundling two projects into one or two
    questions must not leave the other's row behind). De-duplicated on the
    abspath form; a file path mention under a pending boundary is mapped
    UP to that boundary row so '要 index .../core.py' settles the
    project's row, not a no-op (the ledger key is the directory, per the
    owner's 2026-10-13 ruling).
    """
    from .query import _extract_path_mentions
    out: list = []
    qs: list = []
    if isinstance(args, dict):
        raw = args.get("questions")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    qs.append(str(item.get("question") or ""))
                elif isinstance(item, str):
                    qs.append(item)
        if not qs:
            qs = [str(args.get("question") or "")]
    elif isinstance(args, str):
        qs = [args]
    for q in qs:
        for m in _extract_path_mentions(q):
            m_abs = os.path.abspath(m)
            # Map a file mention up to the pending boundary that contains
            # it (the ledger's key is the directory). A mention that
            # contains no pending row is still returned verbatim —
            # clearing an absent row is a no-op, and a new path the user
            # consented to by typing may not be in the ledger yet.
            target = m_abs
            for proj in _pending_projects(session_id):
                if target == proj or target.startswith(proj + os.sep):
                    target = proj
                    break
            if target not in out:
                out.append(target)
    return out


def _clarify_reply_texts(result: Any) -> list:
    """Every question's user_response from a host clarify result payload,
    in question order. A bundled multi-question clarify (other topics
    first, the code_map question second) must not have its code_map
    answer missed just because responses[0] belongs to the other topic
    (reviewer round 1: reading only responses[0] left the pending row
    stuck)."""
    if not result:
        return []
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            parsed = result
    if isinstance(parsed, dict):
        responses = parsed.get("responses")
        if isinstance(responses, list) and responses:
            out = []
            for r in responses:
                if isinstance(r, dict):
                    out.append(str(r.get("user_response") or ""))
                else:
                    out.append(str(r))
            return out
        # Legacy dict shape: {"q0": "..." or {"user_response": "..."}}.
        if isinstance(responses, dict) and responses:
            out = []
            for val in responses.values():
                if isinstance(val, dict):
                    out.append(str(val.get("user_response") or ""))
                else:
                    out.append(str(val))
            return out
        # Legacy single-question shape.
        if parsed.get("user_response"):
            return [str(parsed.get("user_response"))]
        return []
    if isinstance(parsed, list) and parsed:
        return [str(parsed[0])]
    if isinstance(parsed, str):
        return [parsed]
    return []


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
    A bundled multi-question clarify is settled per-question: each
    question's reply is matched against ITS path mentions (reviewer round
    1: reading only responses[0] left the code_map question's answer
    missed when another topic came first). A reply that is '要'-shaped
    ('要', '要了', '要建吧' — any 要-prefix) settles; '不再问'-shaped
    replies clear; '不要'-shaped or free-text answers settle nothing (the
    next read asks again — the owner's '不要' semantics).
    Only SUCCESSFUL clarify calls settle (a cancelled/errored call is not
    an answer — the next turn asks again, same as the evidence gate).
    Fail-soft: a parse hiccup leaves the ledger untouched.
    """
    if status is not None and str(status).lower() not in ("ok", "success", "completed"):
        return
    if not session_id:
        return
    try:
        replies = [r.strip() for r in _clarify_reply_texts(result)]
        for reply in replies:
            if reply.startswith("不再问"):
                projs = _clarify_named_projects(args, session_id=session_id)
                if projs:
                    _clear_project_rows(session_id, projs)
                continue
            # '要'-shaped answer (any 要-prefix: '要', '要了', '要建立'):
            # settle the named project(s). '不要' does NOT start with
            # '要' (it starts with '不'), so no collision.
            if reply.startswith("要"):
                projs = _clarify_named_projects(args, session_id=session_id)
                if projs:
                    _clear_project_rows(session_id, projs)
                continue
            # '不要' / free text / anything else: settle nothing — the
            # owner's "不要 = 下次还问" semantics.
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

    Owner 2026-10-19: a pending project that sits UNDER a confirmed target
    or already has a db record is the "index known but lagging" case —
    refresh it, don't ask "是否建索引". The write side
    (_log_unindexed_read) already skips those rows; this face skips them
    too so a pre-existing row (written before the target was confirmed /
    the db appeared) cannot resurrect a redundant question.
    """
    try:
        if not session_id:
            return None
        projects = [p for p in _pending_projects(session_id)
                    if _needs_index_build_question(p)]
        if not projects:
            return None
        lines = [f"[code_map] 上一轮你读了未索引项目的源码，请对每个项目"
                 f"各发一条 clarify 问用户是否为其建代码地图"
                 f"（推荐一条问题只问一个项目；若合并成一条，代码会按"
                 f"问题里的路径提及逐个清账。三个选项：要 / 不要 / 不再问）："]
        for proj in projects:
            lines.append(f"  - {proj}")
        lines.append("（语义：要 -> 按 code_map 策略取合法的 "
                     "(boundary root, relative path) 对调 "
                     "code_map_check_root(root=<boundary 的父目录>, "
                     "path=<项目目录名>, yes=True) 建索引；"
                     "不要 -> 暂不建，下次读到该项目仍会再问；"
                     "不再问 -> 永不再就该项目询问。）")
        return {"context": "\n".join(lines)}
    except Exception as exc:
        _log.debug("code_map pre_llm_call notice fail-soft: %s", exc)
        return None


def _needs_index_build_question(proj: str) -> bool:
    """True only when the project genuinely needs the '要 / 不要 / 不再问'
    build question. False when the project is already a confirmed target,
    sits under one (refresh case), or has a db record (refresh case).
    Mirrors the write-side skip guards in _log_unindexed_read so a stale
    ledger row can't turn a refresh case into a redundant build question
    (owner 2026-10-19)."""
    if rootreg.is_confirmed_root(proj):
        return False
    confirmed = _confirmed_roots()
    if _find_confirmed_root(proj, confirmed) is not None:
        return False
    try:
        from .config import root_db_path
        if root_db_path(proj).exists():
            return False
    except Exception:
        pass
    return True


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
            # Owner 2026-10-19: a read that just landed inside the ledger
            # under a confirmed target / a db-backed project is the REFRESH
            # case, not a "never indexed" one — drop that session's rows so
            # the turn-boundary question cannot resurrect next turn.
            _purge_obsolete_rows(tool_name, args, _.get("session_id"))
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
        # ── terminal write-signal (owner 2026-10-15 档 2) ──
        # If a terminal command under a confirmed target carries a write/
        # delete signal, mark the enclosing target dirty so the read-side
        # heal (件 3) or an explicit code_map_refresh rebuilds it. No
        # per-file reindex is attempted — the command text cannot be
        # trusted to name which file(s) changed (shell variables, pipes,
        # scripts all defeat that). Fail-soft: any hiccup -> no-op.
        if tool_name == _TERMINAL_TOOL:
            _maybe_mark_terminal_dirty(args, _.get("status"))
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
    PREPENDS to the tool result. P0 PRESERVATION (owner ruling 2026-10-15):
    the host's transform_tool_result REPLACES the result with the first
    string a hook returns, so the returned text must carry the ORIGINAL
    result body verbatim — appending it after the attach block. Returning
    the attach block alone would silently strip the file content from the
    model's view (the exact bug behind 'read_file 被 code_map 劫持').
    When `result` is None/empty (unit-test callers) the attach block is
    returned on its own (backward-compatible).

    terminal read-signal (owner 档 2, 2026-10-15): if the tool is terminal
    and its command is a single-file `cat`/`less` targeting a confirmed
    target source file, append a one-line "you read X.py, use code_map_query
    for its map" nudge to the terminal stdout — deliberately NOT a full
    symbol attach (terminal stdout is opaque; we only point the model).
    Multi-file / piped / grep-style terminal reads get no nudge.

    If the target is dirty, run a synchronous full rebuild first (件 3
    self-heal), then clear the dirty stamp.
    Falls back to the plain nudge text when the query path is unavailable
    (corrupt db / missing venv) — the read result is never broken.
    """
    # terminal read-signal nudge (档 2, owner 2026-10-15): before the
    # read_file/search_files path below, check if this is a terminal tool
    # call whose command is a single-file cat/less of a confirmed-target
    # source file. If so, append a one-line nudge to the terminal stdout.
    if tool_name == _TERMINAL_TOOL:
        targets = _terminal_read_targets(
            str((args or {}).get("command") or ""),
            str((args or {}).get("workdir") or ""),
        )
        if targets:
            rels = [os.path.relpath(t, os.path.dirname(t)) for t in targets]
            nudge = _terminal_read_nudge(targets[0], rels[0])
            if nudge:
                return _with_body(nudge, result)
        return None  # terminal read not matching the narrow sniff -> no nudge
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

    # Search-side directory pointer (owner 2026-10-18): when the tool is
    # `search_files` and its `path` (the search root) is a DIRECTORY that
    # lands under a confirmed target, attach a one-line [code_map] POINTER
    # naming the confirmed root + the `code_map_query` follow-up. A glob
    # against a directory has no per-file symbol table to attach, so this is
    # a POINT (byte-capped), not a 30-symbol block. A `path` that is a
    # concrete FILE keeps falling through to the symbol-attach path below; a
    # bare `pattern` (no absolute `path`) never reaches here (Gate ① above
    # already returned None for it, since a relative pattern abspaths into
    # the CWD and matches no confirmed target — the F6 invariant).
    if tool_name == "search_files" and os.path.isdir(target):
        from .config import SEARCH_DIR_NUDGE_MAX_BYTES
        line = (f"[code_map] your search root {target} is under confirmed "
                f"target {enclosing}; for symbol lookup in this project "
                f"prefer code_map_query(root={enclosing}, name=...) over "
                f"whole-tree glob/grep")
        line = line[:SEARCH_DIR_NUDGE_MAX_BYTES]
        return _with_body(line, result)

    # Must be a source file (extension in INDEXED_EXTENSIONS) — NOT .md/.json
    # and not a binary. Non-source paths under a confirmed target get NO
    # footer at all (owner ruling 2026-10-15): the legacy plain directory
    # nudge hijacked the read result and is retired.
    from .config import INDEXED_EXTENSIONS
    ext = os.path.splitext(target)[1].lower()
    if ext not in INDEXED_EXTENSIONS or _is_binary(target):
        # Owner ruling 2026-10-15 (option A): non-source reads (.md/.json/
        # binary) under a confirmed target get NO footer at all — the plain
        # directory nudge hijacked the read result and is no longer wanted.
        return None
    root = enclosing
    # 件 3: if dirty, heal before attaching (sync full rebuild, ms-scale
    # at ~100-file scale; for larger targets this is the one case where
    # the read path does a full walk — still bounded, and it is the
    # price of "never serve a stale map").
    if rootreg.is_dirty(root):
        _heal_dirty(root)
    attached = _attach_symbols(root, target)
    if attached:
        return _with_body(attached, result)
    return _with_body(_plain_nudge(root), result)  # source file, map query failed -> plain nudge


def _with_body(attach_block: Optional[str], result: Any) -> Optional[str]:
    """P0 preservation (owner ruling 2026-10-15): the host REPLACES the
    tool result with the first string a transform_tool_result hook returns.
    So whatever text we return must END with the original result body,
    verbatim — returning the attach block alone would strip the file
    content from the model's view.

    - `attach_block` None (nothing to attach): return the body itself, or
      None when there is no body (legacy / out-of-scope reads).
    - `result` str: the host's real tool result (JSON string) — append as-is.
    - `result` dict: legacy / unit-test callers — serialize, then append.
    - `result` None / empty: attach block on its own (backward-compatible;
      all pre-P0 tests exercise this shape).
    """
    if not attach_block:
        if isinstance(result, str) and result:
            return result
        return None
    if isinstance(result, str):
        body = result
    elif isinstance(result, dict):
        try:
            body = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            body = ""
    else:
        body = ""
    if not body:
        return attach_block
    return attach_block + "\n" + body


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


# ── terminal-side sniffs (owner 2026-10-15 档 2 不对称版) ────────────────────
# The terminal tool is NOT routed through the write-side reindex path
# (post_tool_call only recognizes write_file/patch) and its stdout is an
# opaque text block (no structured "which file" field the host can use to
# attach a map to). Two narrow, deterministic sniffs close the coverage gap:
#
#   _terminal_has_write_signal(command):
#     conservative string sniff for "this command almost certainly wrote
#     or deleted a file" (redirects, sed -i, tee, rm, git clean -f,
#     python -c "open(...,'w')", …). No shell-AST parsing: the goal is
#     "should we mark the enclosing target dirty?", not "which lines".
#     Missed -> the read-side heal still covers it (correctness);
#     false-positive -> one extra full rebuild (bounded, safe).
#
#   _terminal_read_targets(command, workdir):
#     extracts ONLY the single-file `cat <file>` / `less <file>` targets
#     that land inside a confirmed target AND carry an INDEXED_EXTENSIONS
#     extension. Multi-file cat, pipe/grep output, glob `*.py` — all return
#     [] (deliberately: we refuse to attach to stdout whose source is not
#     a known single source file).

# Shell-level write/delete signal keywords.  A command is "write-shaped"
# if ANY of these tokens appears as a standalone word or as part of a
# Shell write/delete signal detection.  A bare command NAME (rm, tee,
# shred, truncate) must only count when it is a real shell command, NOT
# when it appears inside a quoted argument (`grep -rn "rm " .` is
# read-only and must not fire).  The reviewer's word-boundary attempt
# failed because `\b` treats `"` as a non-word char, so `"rm ` still
# matched.  The fix: mask out quoted regions first, then match command
# names on the mask — so a name is only "live" if it sits outside quotes
# AND is preceded by a command-start boundary (^ / whitespace / & | ; ( ).
#
# Masking rule: replace every `'...'` and `"..."` region with spaces of
# the SAME length (the command-name chars survive, the surrounding
# whitespace does not, so boundaries stay valid but the quoted text
# cannot form a command).  Single-quotes are handled first (shell: no
# escaping inside '), then double-quotes (where backslash can escape ").
def _mask_quoted(cmd: str) -> str:
    out = []
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "'":
            # single-quoted: copy to matching ' (no escaping)
            j = i + 1
            while j < n and cmd[j] != "'":
                j += 1
            out.append(" " * (min(j, n) - i))
            i = j + 1 if j < n else j
        elif ch == '"':
            # double-quoted: backslash escapes the next char (incl. ")
            # EVERY character inside the quotes (except the backslash
            # itself, which we must skip so we don't mask the closing
            # quote prematurely) is masked to a SPACE. This is what
            # makes `grep "a > b" f.py` NOT fire: the `>` sits inside
            # the quotes, so it is a quoted string character, not a
            # shell redirect, and must not count as a write signal.
            j = i + 1
            while j < n:
                if cmd[j] == "\\" and j + 1 < n:
                    j += 2          # skip escaped pair
                    continue
                if cmd[j] == '"':
                    break
                j += 1
            # Mask i .. j (both endpoints exclusive of the closing ")
            # to spaces, then resume just after the closing quote.
            out.append(" " * (j - i))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# Bare write command names, matched on the quote-masked command with a
# command-start boundary.  `rm`, `tee`, `shred`, `truncate`.
_TERMINAL_BARE_WRITE_CMD_RE = re.compile(
    r"(?:^|[\s&|;(>])((?:rm|tee|shred|truncate))(?:[\s]|$)")
# Compound / flag forms: these are specific enough that a read-only
# command mentioning them verbatim is rare, so plain substring is fine
# (they also all carry `-` or multiple words that a bare grep would not
# accidentally reproduce in a quoted string).  Kept as substrings.
_TERMINAL_WRITE_SUBSTRINGS: tuple = (
    "sed -i", "git clean", "git rm",
    "-delete", "-exec rm",
)
# Redirects: `>` or `>>` appearing OUTSIDE quotes (a redirect is the
# actual write op).  Matched on the quote-masked string so `grep "a > b"`
# does NOT fire (the `>` is inside quotes -> masked to spaces).
_TERMINAL_REDIRECT_RE = re.compile(r"(?:^|[\s;&|;($])>{1,2}\s")
# python inline writes:  open(<path>,<mode>) where mode contains w/a/+.
_TERMINAL_PY_WRITE_RE = re.compile(
    r"""open\s*\(\s*["\'][^"\']*["\']\s*,\s*["\'][^"']*[wa+][^"']*["\']""")


def _terminal_has_write_signal(command: str) -> bool:
    """Conservative sniff: does `command` carry a write/delete signal?

    Quote-aware (reviewer 2026-10-15 HIGH): bare command names and
    redirects are only counted when they sit OUTSIDE quoted arguments,
    so `grep -rn "rm " .` and `echo "3 > 2"` do NOT false-positive.
    Fail-soft: empty/None -> False.
    """
    if not command or not isinstance(command, str):
        return False
    masked = _mask_quoted(command)
    if _TERMINAL_BARE_WRITE_CMD_RE.search(masked):
        return True
    if _TERMINAL_REDIRECT_RE.search(masked):
        return True
    for tok in _TERMINAL_WRITE_SUBSTRINGS:
        if tok in masked:
            return True
    if _TERMINAL_PY_WRITE_RE.search(command):
        return True
    return False


def _terminal_read_targets(command: str, workdir: str) -> list:
    """Extract single-file `cat`/`less` source targets from `command` that
    fall under a confirmed target and carry an indexable extension.

    Returns [] in every case where the source file cannot be identified
    with certainty: pipe output, multi-file `cat a.py b.py`, glob
    `*.py`, `grep`/`rg`/`head`/`tail` (these read but their stdout is
    not a whole source file), `vim`/`nano` editors, or any command we
    don't understand.  Only `cat <abs-path-or-rel-to-workdir>` where
    the path resolves to a single file inside a confirmed target with
    an INDEXED_EXTENSIONS extension is returned.
    """
    if not command or not isinstance(command, str):
        return []
    c = command.strip()
    # Reject multi-file cat (two+ non-flag args after cat) — we refuse to
    # attach a map to a concatenated multi-file stdout.
    # Also reject any pipe (`|`) or backtick in the command — the stdout
    # is no longer a whole source file.
    if "|" in c or "`" in c:
        return []
    # Match `cat <path>` as the first meaningful segment (allow leading
    # cd/`cd x && ` prefix but not pipelines).
    m = re.match(r'^(?:cd\s+\S+\s*&&\s*)?(?:cat|less)\s+(\S+)\s*$', c)
    if not m:
        return []
    raw = m.group(1)
    # Only resolve paths that are absolute or clearly relative to workdir;
    # reject glob patterns, redirects, and shell variables.
    if any(ch in raw for ch in "*?{}$`"):
        return []
    target = os.path.abspath(raw) if os.path.isabs(raw) else os.path.join(workdir or ".", raw)
    target = os.path.realpath(target)
    # Must be a single regular file (not a dir)
    if not os.path.isfile(target):
        return []
    from .config import INDEXED_EXTENSIONS
    ext = os.path.splitext(target)[1].lower()
    if ext not in INDEXED_EXTENSIONS or _is_binary(target):
        return []
    # Must land under a confirmed target (symlink-normalized via realpath)
    confirmed = set(rootreg.confirmed_targets())
    if _find_confirmed_root(target, confirmed) is None:
        return []
    return [target]


def _terminal_read_nudge(file_path: str, rel_name: str) -> Optional[str]:
    """档 2 read-side: the one-line "you cat'd a confirmed-target source
    file; use code_map_query for its symbol map" pointer. Deliberately a
    POINT, not a full symbol attach — terminal stdout is opaque (it may be
    build logs, test output, anything) and stuffing 30 symbols into it
    would pollute the model's read of the command output. Byte-capped
    like the cross-folder signal so the follow-up keyword `code_map_query`
    survives truncation. Returns None when nothing confirmed is indexed."""
    from .config import TERMINAL_READ_NUDGE_MAX_BYTES
    confirmed = set(rootreg.confirmed_targets())
    enclosing = _find_confirmed_root(file_path, confirmed)
    if enclosing is None:
        return None
    line = (f"[code_map] you cat'd {rel_name} (under confirmed root "
            f"{enclosing}); for its symbol map call code_map_query(root="
            f"{enclosing}, path={rel_name})")
    return line[:TERMINAL_READ_NUDGE_MAX_BYTES]


def _maybe_mark_terminal_dirty(args: Any, status: Any) -> None:
    """档 2 write-side: if a SUCCESSFUL terminal command with a write signal
    was run with its `workdir` under a confirmed target, mark that target
    dirty so the read-side heal / code_map_refresh rebuilds it before the
    map is trusted again.

    Gated on `status` (ok/success/completed) exactly like the write_file
    reindex path: a failed command has not mutated anything we know about,
    so we must NOT mark the target dirty on error/cancel (the db keeps
    its prior state — the same discipline as 件 1). `workdir` is resolved
    against the confirmed registry via realpath (the symlink fix), so a
    terminal run in a profile-symlink view of a confirmed dir still marks
    the shared physical target.

    Fail-soft: any exception -> no-op (a terminal hiccup never breaks the
    command result reaching the model).
    """
    if status is not None and str(status).lower() not in ("ok", "success", "completed"):
        return
    command = str((args or {}).get("command") or "")
    workdir = str((args or {}).get("workdir") or "")
    if not _terminal_has_write_signal(command):
        return
    if not workdir:
        return
    candidate = os.path.realpath(workdir)
    confirmed = set(rootreg.confirmed_targets())
    root = _find_confirmed_root(candidate, confirmed)
    if root is None:
        return
    rootreg.mark_dirty(root)


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


def _cross_folder_signal(root: str, file_path: str,
                         import_rows: list) -> Optional[str]:
    """Cross-folder dependency existence+count signal (owner 2026-10-13).

    For the read target's in-project import edges, count how many land in a
    DIFFERENT top-level sub-directory of the CONFIRMED root than the file
    itself. This is the "does this file even have cross-folder contacts?"
    question the owner wants the model to see WITHOUT being handed the
    target paths — the count is the signal; `code_map_query` is the follow-up
    for the details. Root-external targets (stdlib / third-party / anything
    that resolves above the confirmed root) are never counted: the confirmed
    root IS the project boundary (owner: "confirm 记录的不就是项目根吗").

    `import_rows` are (name, kind, line, sig, ...) tuples from
    `query_symbols(..., kind="import")`. Resolution is conservative and
    per-file only: the import edge's target module is located among this
    ROOT's indexed files (path = module dotted/relative under root); a
    target that is not indexable inside the root cannot be classified, so it
    is silently dropped (fail-soft, matches the gate ⑨ posture). Returns a
    one-line string when count > 0, else None (no noise line).

    Cost: one sqlite COUNT + a few LIKE lookups on the root's `files`
    table — cheap on the read hot path, bounded by the host hook budget.
    """
    try:
        from .config import root_db_path
        db = root_db_path(root)
        if not db.exists():
            return None
        import sqlite3
        conn = sqlite3.connect(str(db), timeout=2.0)
        try:
            root_abs = os.path.abspath(root)
            file_abs = os.path.abspath(file_path)

            # All in-root module basenames, to classify import edges.
            indexed = [r[0] for r in conn.execute(
                "SELECT path FROM files").fetchall()]
            mod_to_files = {}
            def _real_module(name: str, sig: str) -> Optional[str]:
                """Recover the REAL imported symbol from an import edge.

                For `from ..sub_b import util as tool`, the parser records
                the ALIAS `tool` as the import name, but the module file is
                `util.py` — which is what the basename map keys on. The `sig`
                field carries the full line ('from ..sub_b import util as
                tool'), so we recover the original symbol without a schema
                change. For non-aliased edges the recorded `name` already IS
                the imported symbol, so it is returned as-is. Return None
                when the edge can't be classified as an in-root module.
                """
                if not sig:
                    return name
                s = sig.strip()
                if s.startswith("from "):
                    # `from <mod> import <sym> [as <alias>]`
                    # The imported symbol is the FIRST name AFTER 'import'.
                    parts = s.split()
                    try:
                        imp_idx = parts.index("import")
                    except ValueError:
                        return name
                    if len(parts) > imp_idx + 1:
                        sym = parts[imp_idx + 1]
                        # strip surrounding parens (e.g. `import (a, b)`)
                        sym = sym.lstrip("(")
                        return sym or name
                    return name
                # `import <mod>` — name IS the module/symbol
                return name

            # Python-only signal (reviewer round-1, finding ④): the basename
            # module map keys on the `.py` extension; a non-Python file's
            # import edges (TS/Go/Rust etc.) therefore never resolve and are
            # silently skipped (fail-soft). This is deliberate — the signal
            # is a cheap existence hint, not a full cross-language resolver.
            for p in indexed:
                b = os.path.basename(p)
                if b.endswith(".py"):
                    b = b[:-3]
                # Store the TOP-LEVEL sub-dir of the confirmed root (e.g. 'a'
                # for 'a/b/x.py'), not the full relative dir. The owner's
                # cross-folder definition is relative to the confirmed root:
                # 'a/b/x.py' vs 'a/c/x.py' are NOT cross-folder (both under
                # top-level 'a'), while 'a/b/x.py' vs 'sub_b/x.py' ARE.
                rel_dir = os.path.dirname(os.path.relpath(os.path.abspath(p), root_abs))
                top_dir = rel_dir.split(os.sep, 1)[0] if rel_dir else ""
                mod_to_files.setdefault(b, []).append(top_dir)

            count = 0
            seen = set()
            for r in import_rows:
                name = r[2]
                sig = r[5] if len(r) > 5 else ""
                # Reveal the real module name (alias -> original) so an
                # `as`-renamed edge still matches the basename map.
                real = _real_module(name, sig)
                if not real or real in seen:
                    continue
                seen.add(real)
                dirs = mod_to_files.get(real)
                if not dirs:
                    continue  # not indexable inside this root -> stdlib/3rd
                # Cross-folder iff SOME directory of `real` differs from the
                # file's own top-level sub-dir (handles same-basename-in-
                # multiple-subdirs = inherently cross-folder).
                own_top = os.path.dirname(os.path.relpath(
                    os.path.abspath(file_path), root_abs)).split(os.sep, 1)[0] \
                    if os.path.dirname(os.path.relpath(os.path.abspath(file_path), root_abs)) else ""
                if any(d != own_top for d in dirs):
                    count += 1
        finally:
            conn.close()
    except Exception as exc:
        _log.debug("code_map cross-folder signal fail-soft: %s", exc)
        return None
    if count <= 0:
        return None
    line = (f"  cross-folder: {count} import target(s) in other sub-dir(s) "
            f"of this confirmed root (count over the {len(import_rows)} "
            f"import edge(s) attached above; more imports may exist beyond "
            f"top-N -> code_map_query for the full list)")
    line = line[:CROSS_FOLDER_LINE_MAX_BYTES]
    return line


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
    # Cross-folder existence signal (owner 2026-10-13): appended AFTER the
    # byte cap, OUTSIDE READ_SYMBOL_MAX_BYTES — it must never squeeze the
    # symbol lines. Emitted only when N > 0 (no noise line when 0).
    cross = _cross_folder_signal(root, file_path, import_rows)
    if cross:
        out = out + "\n" + cross
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
      P0: `result` (the host's original tool result string) is forwarded so
      _maybe_nudge can PREPEND the attach block to a verbatim copy of it —
      the host replaces the result with the first string we return, so
      dropping `result` here would strip the file content from the model's
      view (owner ruling 2026-10-15).

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
    """Return the confirmed target that `file_path` falls under (nearest).

    Normalizes BOTH the candidate file path and each confirmed target via
    `os.path.realpath` (resolving symlinks to their physical target). This is
    critical for the profile/symlink deployment model: a profile-local dir
    like /root/.hermes/profiles/<p>/plugins/code_map is a SYMLINK to the
    shared physical /root/.hermes/plugins/code_map; abspath alone keeps the
    symlink prefix, so the string-prefix match against the shared target in
    the registry never fires, the read is treated as "outside any confirmed
    target" and the un-indexed-read ledger gets a spurious row for it.
    realpath collapses both sides to the same physical dir and fixes the
    match. (The registry still stores logical confirmed-target strings; only
    the read-side matching normalizes.)"""
    abspath = os.path.realpath(file_path)
    best = None
    for root in confirmed:
        root_abs = os.path.realpath(root)
        if abspath.startswith(root_abs + os.sep) or abspath == root_abs:
            if best is None or len(root_abs) > len(best):
                best = root_abs
    return best
