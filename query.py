"""query.py — code_map tool handlers (L1.5 cross-file symbol location).

Gate 10 + owner rule (2026-10-11): a root is only valid if it is
confirmed in the registry (rootreg). Root and path are separate
concerns: a root-only or path-only call is invalid, and a path must
resolve within the confirmed root. No CWD fallback anywhere.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import PAGE_BUDGET_BYTES, PAGE_MAX_SYMBOLS, data_dir, root_db_path
from .db import init_db, query_symbols, stale_paths
from .fingerprint import diff_indexed, invalidate_cache, on_disk_files
from . import rootreg

# ── transcript 兜底 (Q3, owner policy 2026-10-11) ─────────────────────────
# 宿主 clarify 工具是原生单选卡；其应答在 transcript（session 持久层）里
# 留痕。插件侧没有宿主 UI 通道，"用户点头"的权威记录就是 evidence log：
#   1. 模型在 code_map 目标上发起宿主 clarify 时，post_tool_call hook
#      （on_clarify_post，见 hook.py）把 (目标, 用户应答, 时间) 追加进
#      data_dir()/_clarify_evidence.jsonl —— 这是"transcript"里插件可
#      信、可审计的那份留痕（session DB 属宿主，插件不直接读）。
#   2. code_map_check_root(yes=true) 前扫描该 log：同一目标存在肯定应答
#      才放行 build，并把证据写进 audit jsonl（transcript_evidence 字段）；
#      否则 fail-closed 拒绝（blocked_by_policy），指引"先 clarify 再
#      yes=true"。382k-file 事故的同类风险面由此闭环。
_EVIDENCE_FILE = "_clarify_evidence.jsonl"

# 宿主单选卡应答的肯定词面（clarify choices 约定：确认保存/确认/yes/...）。
# 保守匹配：只有明确肯定词才放行，含糊/否定/其他一律视为无证据（fail-closed）。
_AFFIRMATIVE = ("确认", "确认保存", "同意", "批准", "可以", "好", "好的", "行",
                "动手", "yes", "y", "ok", "okay", "approve", "approved", "go")


def _evidence_log() -> Path:
    return data_dir() / _EVIDENCE_FILE


def _is_affirmative(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    # 多词面：整串命中，或作为独立词命中（防 "不确认" 里带 "确认" 的误放行）
    for w in _AFFIRMATIVE:
        if t == w.lower():
            return True
    # 短应答（单选卡点选）整串比较已覆盖；其余自由文本不猜。
    return False


def _clarify_evidence(target: str, root: str, path: str) -> Optional[Dict[str, Any]]:
    """Latest transcript evidence that the user affirmatively confirmed
    THIS exact target via the host clarify tool. Returns the evidence
    record (with ts + user response) or None (no/affirmative-missing).
    Fail-soft: a missing/corrupt log -> None (fail-closed at the gate)."""
    target = os.path.abspath(target)
    best: Optional[Dict[str, Any]] = None
    log = _evidence_log()
    if not log.exists():
        return None
    try:
        for line in log.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("target") != target:
                continue
            if not rec.get("affirmative"):
                continue
            # Newest first wins (ts is host-local ISO from the hook).
            if best is None or str(rec.get("ts", "")) >= str(best.get("ts", "")):
                best = rec
    except OSError:
        return None
    return best


def record_clarify_evidence(target: str, user_response: str,
                            ts: Optional[str] = None) -> None:
    """Append one clarify-evidence record (called by the post_tool_call
    hook for host clarify calls). Appends ONLY — the gate reads newest.
    Fail-soft: a log write hiccup must never break a tool call."""
    if not target:
        return
    rec = {
        "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "target": os.path.abspath(target),
        "user_response": (user_response or "")[:400],
        "affirmative": _is_affirmative(user_response or ""),
    }
    try:
        log = _evidence_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _invalid(note: str, limit: int) -> str:
    """Uniform rejection payload: no rows, no query_ms, status='invalid'."""
    return json.dumps({
        "symbols": [], "count": 0, "limit": limit, "cursor": "0",
        "staleness": "fresh", "status": "invalid", "note": note,
    }, ensure_ascii=False)


def _resolve_query_path(root: str, path: str) -> Optional[str]:
    """Resolve `path` against `root`; return None if it escapes the root.

    Owner's rule (2026-10-11): a path that does not lie under root is
    invalid, so system-dir paths (/etc/passwd, ...) can never be
    reached through the query surface."""
    if os.path.isabs(path):
        resolved = os.path.abspath(path)
    else:
        resolved = os.path.abspath(os.path.join(root, path))
    if resolved != root and not resolved.startswith(root + os.sep):
        return None
    return resolved


def _staleness(root: str, conn) -> str:
    """Check if the index for `root` is stale (files changed since last build).

    Returns 'fresh' | 'partial'. Exception -> 'fresh' (non-blocking disclosure,
    not a rescan signal).
    """
    try:
        on_disk = on_disk_files(root)
        added, removed = diff_indexed(root, on_disk)
        if added or removed:
            return "partial"
        return "fresh"
    except Exception:
        return "fresh"


def _serialize(page: list, staleness: str, root: str,
               limit: int, cursor: str) -> str:
    # query_symbols row shape: (root, path, name, kind, line, sig)
    rows = [
        {"root": r[0], "path": r[1], "name": r[2],
         "kind": r[3], "line": r[4], "sig": r[5], "staleness": staleness}
        for r in page
    ]
    payload: Dict[str, Any] = {
        "symbols": rows,
        "count": len(rows),
        "limit": limit,
        "cursor": str(cursor),
        "staleness": staleness,
    }
    # Go/Rust disclosure: method names are unqualified; Rust methods
    # surface as kind=function (L113 disclosure line).
    # L2 call_site: name-level approximation — receiver-qualified calls
    # (`obj.m()`, `std::puts()`) yield only the called name ("m", "puts"),
    # not the fully-qualified symbol.
    payload["disclosure"] = (
        "Go/Rust: method names are unqualified; Rust methods "
        "surface as kind=function. "
        "call_site rows are name-level: `obj.m()` -> 'm', "
        "`std::puts()` -> 'puts' (receiver/namespace not resolved)."
    )
    return json.dumps(payload, ensure_ascii=False)


def code_map_query(args: Dict[str, Any]) -> str:
    """code_map_query tool handler. Paged symbol lookup.

    Owner's rule (2026-10-11): root and path are separate concerns.
    - root is REQUIRED and must be a confirmed root (rootreg); no
      CWD fallback — omitting it is invalid.
    - With a root, at least one of name/path must be present; a bare
      root has nothing to locate.
    - path must resolve within root; absolute paths pointing outside
      (e.g. /etc/passwd) are rejected."""
    name = args.get("name")
    path = args.get("path")
    kind = args.get("kind")
    limit = args.get("limit", PAGE_MAX_SYMBOLS)
    cursor = args.get("cursor", "0")

    # --- root gate: required + confirmed (registry is the single source) ---
    root = args.get("root") or ""
    if not root:
        return _invalid(
            "root required: pass a confirmed project root "
            "(a path-only call is invalid)", limit)
    root = os.path.abspath(root)
    if not rootreg.is_confirmed_root(root):
        return _invalid(
            f"root '{root}' is not confirmed; call code_map_confirm_root "
            "first", limit)

    # --- name/path gate: at least one, and path must stay in-root ---
    if not name and not path:
        return _invalid(
            "nothing to locate: provide name, path, or both "
            "(a root-only call is invalid)", limit)

    path_filter: Optional[str] = None
    if path:
        resolved = _resolve_query_path(root, path)
        if resolved is None:
            return _invalid(
                f"path '{path}' does not lie within root '{root}'; "
                "out-of-root and system-dir paths are rejected", limit)
        path_filter = resolved

    db_path = root_db_path(root)
    if not db_path.exists():
        return json.dumps({
            "symbols": [], "count": 0, "limit": limit,
            "cursor": "0", "staleness": "partial",
            "note": "index not built for this root yet; "
                    "call code_map_refresh to re-scan",
        }, ensure_ascii=False)

    conn = init_db(root)
    try:
        t0 = time.perf_counter()  # L45
        staleness = _staleness(root, conn)  # L64
        offset = int(cursor) if cursor.isdigit() else 0
        rows = query_symbols(conn, root, name=name, path=path_filter,
                             kind=kind, limit=limit, offset=offset)
        page = rows[:PAGE_MAX_SYMBOLS]
        elapsed_ms = int((time.perf_counter() - t0) * 1000)  # L90

        result = _serialize(page, staleness, root, limit,
                            str(offset + limit))

        # Single-page byte budget: truncate if over PAGE_BUDGET_BYTES
        if len(result.encode("utf-8")) > PAGE_BUDGET_BYTES:
            while len(page) > 1:
                page = page[:-1]
                result = _serialize(page, staleness, root, limit,
                                    str(offset + len(page)))
                if len(result.encode("utf-8")) <= PAGE_BUDGET_BYTES:
                    break

        payload = json.loads(result)
        payload["query_ms"] = elapsed_ms
        return json.dumps(payload, ensure_ascii=False)
    finally:
        conn.close()


def code_map_check_root(args: Dict[str, Any]) -> str:
    """code_map_check_root: safe confirm gate (2B).

    Same (root, path) pair contract as confirm_root. Dry-run by
    default: reports what WOULD be indexed at the joined target,
    writes nothing. `yes=True` lifts to an actual build and appends
    ONE audit line to data/confirm_root_log.jsonl. Gate 10 (v2
    concatenation rule) applies before any filesystem walk."""
    import datetime
    import hashlib

    from .state import _gate10
    root = args.get("root", "")
    path = args.get("path", "")
    if not root and args.get("root_path"):
        root = args["root_path"]
    yes = bool(args.get("yes"))

    gate = _gate10(root, path)
    if gate is not None:
        gate["dry_run"] = not yes
        return json.dumps(gate, ensure_ascii=False)

    from . import rootreg
    target = rootreg._join_target(root, path)
    assert target is not None  # gate passed above; _join_target is not None
    from .config import INDEXED_EXTENSIONS, data_dir
    from .fingerprint import on_disk_files

    if os.path.isdir(target):
        indexable = [p for p in on_disk_files(target)
                     if os.path.splitext(p)[1].lstrip(".").lower() in
                     {e.lstrip(".") for e in INDEXED_EXTENSIONS}]
    else:
        indexable = []

    if not yes:
        return json.dumps({
            "root": target, "dry_run": True, "files_indexable": len(indexable),
            "note": "dry-run: nothing indexed. Pass yes=true to build.",
        }, ensure_ascii=False)

    # ── transcript 兜底（Q3, owner 定：模型推 clarify，transcript 兜底）──
    # 宿主 clarify 工具是原生单选卡，_sum_clarify 在 context_compressor 里
    # 保证 clarify 的问答对（user_response）压缩后仍留痕。插件侧没有宿主
    # UI 通道，"用户点头"的权威记录就是 transcript 里那次 clarify 的应答。
    # 所以 yes=true 前先做 transcript 扫描：
    #   本会话对同一 target 存在一条 clarify 记录、且用户应答是肯定的
    #   （yes/confirm/approve 等；宿主应答文本在 transcript 里可检索）
    #   → 放行 build，并把该证据写进 audit jsonl（transcript_evidence 字段）。
    #   找不到肯定应答 → 拒绝 build，返回 blocked_by_policy + 明确的
    #   "先 clarify 再 yes=true" 指引。这是 fail-closed：宁可挡一次 build，
    #   不放行一个没有用户点头的 confirm（382k-file 事故的同类风险面）。
    evidence = _clarify_evidence(target, root, path)
    if evidence is None:
        return json.dumps({
            "status": "blocked_by_policy",
            "root": target,
            "note": ("yes=true requires a prior user confirmation for this "
                     "exact target. Call code_map_check_root (dry-run) and "
                     "ask the user via the host `clarify` tool first; the "
                     "user's affirmative reply is the transcript evidence "
                     "this gate accepts. (Owner policy 2026-10-11: 模型推 "
                     "clarify, transcript 兜底.)"),
            "next": "code_map_check_root(root, path) + clarify, then yes=true",
        }, ensure_ascii=False)

    # Confirm-gated build
    from .state import confirm_root
    res = confirm_root(root, path)
    if res.get("status") != "build_started":
        return json.dumps(res, ensure_ascii=False)

    # Audit trail (Step 3, mandatory on every successful confirm). The
    # transcript evidence (which clarify reply justified this build) is
    # written INTO the audit line, so a later reviewer can replay the
    # confirmation chain from data/confirm_root_log.jsonl alone.
    digest = hashlib.sha1(target.encode("utf-8")).hexdigest()[:16]
    log_path = data_dir() / "confirm_root_log.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(),
            "action": "confirm_root",
            "root": target,
            "boundary": root, "rel_path": path,
            "files_indexed": len(indexable),
            "db_path": f"{digest}.db",
            "transcript_evidence": evidence,
        }) + "\n")

    res["files_indexed"] = len(indexable)
    res["dry_run"] = False
    res["audit"] = str(log_path)
    return json.dumps(res, ensure_ascii=False)


def code_map_confirm_root(args: Dict[str, Any]) -> str:
    """code_map_confirm_root: lock a (root, path) target and build.

    Owner's rule (2026-10-11 v2): root AND path are both mandatory.
    root is the boundary prefix (/root, /usr, /home, ...); path is
    the RELATIVE sub-path inside it. The joined target is what gets
    indexed and registered. A root-only call (whole-boundary index —
    the 382k-file incident) or a path-only call (no boundary) is
    rejected; the bare filesystem root '/' is rejected; a path that
    escapes the boundary via ../ is rejected."""
    from .state import confirm_root
    root = args.get("root", "")
    path = args.get("path", "")
    # Legacy single-arg call shape: root_path= full target, no
    # relative path — accepted ONLY as an already-joined target,
    # which means root=root_path, path="" — that FAILS the gate, so
    # legacy callers must migrate to the (root, path) pair.
    if not root and args.get("root_path"):
        root = args["root_path"]
    result = confirm_root(root, path)
    return json.dumps(result, ensure_ascii=False)


def code_map_refresh(args: Dict[str, Any]) -> str:
    """code_map_refresh: authoritative re-scan (async, non-blocking).

    Same (root, path) pair shape as confirm_root; the joined target
    must already be confirmed. A bare full-target call (root_path=
    stored target, no relative path) is also accepted for
    refreshing an existing registry entry."""
    from .state import refresh_root
    root = args.get("root", "")
    path = args.get("path", "")
    if not root and args.get("root_path"):
        root = args["root_path"]
    result = refresh_root(root, path)
    return json.dumps(result, ensure_ascii=False)
