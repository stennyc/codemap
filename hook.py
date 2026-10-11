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

def _record_clarify_evidence(args: Any, result: Any, status: Any) -> None:
    """Record a host clarify call as transcript evidence for the code_map
    target it asked about (Q3, owner policy: 模型推 clarify, transcript 兜底).

    Shape (verified against the host clarify tool):
      args: {"question": "...", "choices": [...]}  — the question is
            REQUIRED to mention the code_map target (the policy text
            instructs the model to phrase it as: "要 index <target> 吗？"
            so the target path is in the question).
      result: the user's reply (a choice label or free text), possibly
            JSON-encoded depending on the host renderer.

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
        q = (args or {}).get("question") if isinstance(args, dict) else None
        if not q:
            return
        # The user's reply: result is the tool's result payload (str or
        # JSON str). Extract the reply TEXT (not its JSON encoding) so the
        # affirmative match sees "确认", not the quoted '"确认"'.
        reply = ""
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except ValueError:
                parsed = result
            if isinstance(parsed, dict):
                reply = str(parsed.get("response", ""))
            elif isinstance(parsed, list) and parsed:
                reply = str(parsed[0])
            else:
                reply = str(parsed)
        elif isinstance(result, dict):
            reply = str(result.get("response", ""))
        if not reply:
            return
        # Join key: which confirmed target does the question ask about?
        # Rule (shared with the gate-side lookup, RISK #6 fallback): the
        # question may mention the target verbatim OR by a trailing sub-path
        # of >= 2 consecutive path segments (e.g. 'plugins/code_map' for a
        # deep target) — that is enough for the join; a bare single segment
        # is not. Longest-matching target still wins.
        best: Optional[str] = None
        from .query import target_match
        for target in rootreg.confirmed_targets():
            if target and target_match(q, target):
                if best is None or len(target) > len(best):
                    best = target
        if best is None:
            return
        from .query import record_clarify_evidence
        record_clarify_evidence(best, reply)
    except Exception as exc:
        _log.debug("code_map clarify evidence record fail-soft: %s", exc)


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
        # Q3 transcript 兜底：宿主 clarify 工具的一次调用 = "用户被问到且
        # 已应答"的权威 transcript 证据。args 里携带 code_map 目标
        # （策略文本要求模型把目标写进 clarify 的问题里），result 里是
        # 用户的应答文本。两者齐全且调用成功才落 evidence log。
        if tool_name == "clarify":
            _record_clarify_evidence(args, result, _.get("status"))
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
