"""query.py — code_map tool handlers (L1.5 cross-file symbol location).

Gate 10 + owner rule (2026-10-11): a root is only valid if it is
confirmed in the registry (rootreg). Root and path are separate
concerns: a root-only or path-only call is invalid, and a path must
resolve within the confirmed root. No CWD fallback anywhere.
"""
from __future__ import annotations

import json
import os
import re
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
_AFFIRMATIVE = frozenset({
    # Chinese
    "确认", "确认保存", "同意", "批准", "可以", "好", "好的", "行", "动手",
    "就按这个来",
    # English (incl. the colloquial forms RISK #5 flagged as missed)
    "yes", "y", "ok", "okay", "approve", "approved", "go",
    "sure", "yep", "yeah", "sounds good", "sounds fine", "great", "go for it",
})

_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_]+|[\u4e00-\u9fff\u3400-\u4dbf]+"
)

# RISK #1 (reviewer deleg_1993fe7a, 2026-10-12): mixed-token collision.
# The token matcher above returns True as soon as ANY affirmative token
# appears, so "确认 取消" (user typed both, or a copy-paste artifact) was
# misjudged as consent. A reply that also contains an explicit NEGATIVE
# token is not a clean yes — short-circuit to False before consulting
# the affirmative word list. Negatives are matched as whole tokens too
# (not raw substrings), so "可以" inside "不可以" is already safe via
# the CJK-run token rule and this list adds the explicit "user said no
# in the same reply" case.
# Negative tokens (ASCII) — matched as whole tokens against _TOKEN_RE,
# same rule as the affirmative list (no raw-substring matching, so "no"
# inside "non" / "now" never trips it).
_NEGATIVE_ASCII = frozenset({
    "no", "nope", "nvm", "not now", "reject", "denied", "deny", "stop",
    "canc", "cancel",
})
# CJK multi-char negation markers — matched as plain substrings of the
# lowercased text (deliberately NOT whole-token): a CJK run like
# "不过先取消" or "先不建" hides the marker inside a longer run, and
# these specific multi-char words are unambiguous enough that a substring
# hit IS a real negation. Single-char "不" is NOT in this set (too many
# false-positive substrings: "不行" inside "不太行得通" etc. — keep the
# token-level CJK guard for that one instead).
# RISK note (reviewer, 2026-10-12): '否决' as a plain substring can, in
# principle, misfire on a sentence like '否决不了，还是确认吧' (a genuine
# affirmation that happens to contain the word). Deliberately accepted,
# not 'fixed': the failure mode is fail-closed (a real yes gets blocked,
# the user just has to re-ask), and this phrasing is rare enough that
# adding carve-out logic for it isn't worth the complexity. Documented
# so nobody 'fixes' it the wrong way later.
_NEGATIVE_CJK_SUBSTR = ("取消", "拒绝", "不要", "不用", "不建", "不索引", "否决")

# RISK #6 fallback: a clarify question may mention the target by a TRAILING
# sub-path (>= 2 consecutive path segments) instead of the full abspath;
# both the hook (record side) and the gate (lookup side) use this rule.
_MIN_PARTIAL_MATCH_SEGMENTS = 2


def _evidence_log() -> Path:
    return data_dir() / _EVIDENCE_FILE


def _path_segments(target: str):
    return [s for s in target.replace("\\", "/").split("/") if s]


def target_match(question: str, target: str,
                 allow_partial: bool = True) -> bool:
    """Does `question` reference `target`?

    Primary: the full abspath appears verbatim (original, strict rule) —
    always accepted, `allow_partial` irrelevant.
    Fallback (RISK #6): a TRAILING run of >= 2 consecutive path segments of
    `target` appears contiguously in the question — the realistic case where
    the model says 'plugins/code_map' instead of the full abspath. A single
    bare segment is NOT enough (collision-prone: any project named
    'code_map' would match).

    `allow_partial` (default True): whether the trailing-sub-path fallback is
    consulted at all. The hook passes False when it is the NEW-PATH fallback
    arm (a question that mentioned no confirmed target at all): in that
    arm a bare 'x/y' mention must NOT be enough to create evidence for some
    arbitrary path the user never discussed — only a verbatim full-target
    mention counts there (a path the user spelled out completely in the
    question IS a deliberate target reference; a 2-segment relative
    mention of an unknown path is not). The confirmed-target arm keeps
    True, since a trailing-sub-path mention of an ALREADY-confirmed target
    is safe (that target already passed a prior user confirm)."""
    q = (question or "")
    t = (target or "").replace("\\", "/")
    if not q or not t:
        return False
    if t in q:
        return True
    if not allow_partial:
        return False
    segs = _path_segments(t)
    if len(segs) < _MIN_PARTIAL_MATCH_SEGMENTS:
        return False
    # A TRAILING run (suffix) of >= _MIN_PARTIAL_MATCH_SEGMENTS consecutive
    # segments of the target appearing contiguously in the question is
    # enough. Checked longest-suffix-first; the "last N segments" phrasing
    # is the realistic model case ('plugins/code_map' for a deep target).
    for nseg in range(len(segs), _MIN_PARTIAL_MATCH_SEGMENTS - 1, -1):
        if "/".join(segs[-nseg:]) in q:
            return True
    return False


def _is_affirmative(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    # RISK #1 negative short-circuit: an explicit negative in the reply
    # voids any affirmative in the same reply (mixed-token collision,
    # e.g. "确认 取消" / "yes but no"). Two matching rules, chosen per
    # language family (see _NEGATIVE_* comments): ASCII negatives as
    # whole tokens, CJK multi-char negation markers as plain substrings.
    if any(marker in t for marker in _NEGATIVE_CJK_SUBSTR):
        return False
    tokens = _TOKEN_RE.findall(t)
    if any(tok in _NEGATIVE_ASCII for tok in tokens):
        return False
    # Whole-string match (the single-choice-tap case).
    if t in _AFFIRMATIVE:
        return True
    # Token-boundary match (RISK #5): phrase/paragraph replies count ONLY
    # when an affirmative word stands as its own token — never as a raw
    # substring, preserving the "不确认" / "不可以" guard. ASCII tokens are
    # word runs; CJK tokens are single CJK chars/punct runs.
    return any(tok in _AFFIRMATIVE for tok in tokens)


_ABS_PATH_RE = None

def _extract_path_mentions(question: str) -> List[str]:
    """Every ABSOLUTE-path mention (starts with '/' in the question text,
    >= 2 '/'-separated segments) found in a free-form question string,
    longest-first. Absolute mentions are explicit, deliberate path
    references — the realistic case for the hook's new-path evidence
    fallback. A relative fragment ('x/y') is deliberately NOT returned:
    a bare fragment of a never-seen target must not mint evidence (the
    collision risk that made the first, too-loose fix wrong). Pure text,
    no filesystem access, no side effects.

    Cross-platform note (reviewer RISK #4, 2026-10-12): this extractor
    is POSIX-path-shaped and returns whatever raw text is in the
    question, UNNORMALIZED — callers that compare these mentions
    against each other (or against a stored target) must do so on the
    os.path.abspath() form, not the raw string, or distinct raw
    spellings of the same target (../ folds, double slashes, trailing
    spaces) will not dedupe. On Windows an 'absolute' path would start
    with a drive letter, not '/', so this extractor would simply find
    no mentions there — a real Windows deployment would need a
    drive-letter-aware variant; not a concern for this host (Linux)
    today, documented so nobody silently 'fixes' it by swapping in a
    regex that also matches relative fragments."""
    global _ABS_PATH_RE
    if _ABS_PATH_RE is None:
        import re as _re
        # NOTE: the character class deliberately EXCLUDES whitespace —
        # that's what makes the "trailing-space variant" dedup case
        # (reviewer RISK #2, 2026-10-12) work: a reply that types
        # "/a/b/c " (trailing space) extracts as "/a/b/c", whose
        # os.path.abspath() matches the question's clean "/a/b/c".
        # If anyone ever adds \s to this class, that dedup silently
        # breaks (raw-text comparison would see two distinct strings).
        _ABS_PATH_RE = _re.compile(
            r"/(?:[A-Za-z0-9._-]+/){1,}[A-Za-z0-9._-]+")
    found = set(_ABS_PATH_RE.findall(question or ""))
    return sorted(found, key=len, reverse=True)



def _clarify_evidence(target: str, root: str, path: str) -> Optional[Dict[str, Any]]:
    # NOTE: `root`/`path` are unused (kept for call-site signature
    # compatibility); the gate target is the joined+abspath-normalized
    # `target`, which is the ONLY join key consulted against the log.
    del root, path
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
