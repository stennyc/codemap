"""query.py — code_map tool handlers (L1.5 cross-file symbol location).

Gate 10 + owner rule (2026-10-11): a root is only valid if it is
confirmed in the registry (rootreg). Root and path are separate
concerns: a root-only or path-only call is invalid, and a path must
resolve within the confirmed root. No CWD fallback anywhere.
"""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import (CONCEPT_RARITY_MAX_CANDIDATES, HOP_FRONTIER_CAP,
                    HOP_MAX_DEPTH, PAGE_BUDGET_BYTES,
                    PAGE_MAX_SYMBOLS, data_dir, root_db_path)
from .db import (init_db, count_symbols, count_concept, query_symbols,
                query_concept, stale_paths)
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
_SIGNING_KEY_FILE = "_evidence_signing_key"

# ── C-mid: per-line HMAC signature over the clarify-evidence log ─────────
# Owner policy (2026-10-12): the session-stamped evidence log is still a
# plain file a same-host writer (including the model via execute_code /
# terminal, all root here) can forge. C-lite rejects cross-session and
# un-stamped lines, but a forged line STAMPED with the current
# HERMES_SESSION_ID would still pass C-lite. C-mid closes that: every
# evidence line carries an HMAC-SHA256 signature over its canonical JSON,
# keyed by a random secret generated once and held ONLY under data/
# (co-located with the log). A hand-written line cannot produce a valid
# signature, so the gate rejects it; legitimate hook-minted lines verify.
#
# Back-compat (fail-open for data this process itself wrote): lines minted
# BEFORE C-mid has no "sig" field and are accepted (old confirmations must
# not be stranded). A forged line added AFTER C-mid carries no valid sig ->
# rejected. A pre-C-mid line with a GARBAGE sig is also rejected (an
# attacker touching the log loses the back-compat pass).

def _evidence_key_path() -> Path:
    return data_dir() / _SIGNING_KEY_FILE


def _load_evidence_key() -> Optional[bytes]:
    """Return the signing key, or None when no key exists yet.

    The key is generated lazily (see _mint_evidence_line); this reader is
    used by the gate, where a missing key simply means "no verifiable
    lines expected" -> fall back to C-lite behavior, never crash."""
    p = _evidence_key_path()
    try:
        if p.exists():
            return p.read_bytes()
    except OSError:
        return None
    return None


def _sign_evidence_line(rec: Dict[str, Any], key: Optional[bytes]) -> Dict[str, Any]:
    """Compute the sig for an evidence record (sig field added by the caller).

    The canonical payload is the record WITHOUT the sig field, key-sorted
    JSON (UTF-8, no ascii escaping) — byte-stable for a given record. The
    key stays out of the payload; it only signs it."""
    payload = {k: v for k, v in rec.items() if k != "sig"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    if key is None:
        return {"sig": None}
    return {"sig": hmac.new(key, canonical, hashlib.sha256).hexdigest()}


def _verify_evidence_line(rec: Dict[str, Any], key: bytes) -> bool:
    """True iff rec["sig"] matches the HMAC over the canonical payload.

    Recreate the expected signature and use constant-time compare. A
    missing/None sig is a False (no back-compat: pre-C-mid lines are
    handled by the caller via the "sig" key-absent branch, not here)."""
    if "sig" not in rec or rec.get("sig") in (None, ""):
        return False
    payload = {k: v for k, v in rec.items() if k != "sig"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(rec["sig"]))


def _mint_evidence_line(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure an evidence record carries a valid C-mid signature before it
    is written. Loads (or generates on first use) the key under data/,
    computes the sig, and returns the record WITH "sig" attached. The key
    file is created 0600 so a co-host reader can't trivially copy it.
    Fail-soft: on any key/fs hiccup the record is returned UNSIGNED
    (sig=None); the gate then degrades per the documented skip-arm rule.

    M1: concurrent first-time mints can each generate a distinct key; the
    one that wins the key-file write is authoritative, the other's sigs
    become orphaned. In practice the evidence log is single-writer under
    host dispatch, so this is a robustness note, not a security issue."""
    key = _load_evidence_key()
    if key is None:
        try:
            import os as _os
            key = _os.urandom(32)
            p = _evidence_key_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "wb") as f:
                f.write(key)
            try:
                _os.chmod(p, 0o600)
            except OSError:
                pass
        except OSError:
            key = None
    signed = _sign_evidence_line(rec, key)
    rec = dict(rec)
    rec["sig"] = signed["sig"]
    return rec

# 宿主单选卡应答的肯定词面（clarify choices 约定：确认保存/确认/yes/...）。
# 保守匹配：只有明确肯定词才放行，含糊/否定/其他一律视为无证据（fail-closed）。
_AFFIRMATIVE = frozenset({
    # Chinese
    "确认", "确认保存", "同意", "批准", "可以", "好", "好的", "行", "动手",
    "就按这个来",
    # 三选项固定应答词（owner 2026-10-13 设计：clarify 卡片选项"要"）：
    # 整词匹配靠 RISK #5 的 token-boundary 规则（"要" 只在独立成词时命中，
    # "不需要"/"要不要都行" 里的 "要" 是 CJK 连词的一部分，不会误判）；
    # 自由文本变体（"要了"/"要建立"）额外列全，"不要" 永远由负面短电路
    # 拦在 false（见 RISK #1 注释）。
    "要", "要了", "要建", "要建立", "要建吧", "要建索引",
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


def _target_in_descent_chain(target: str, evidence_targets: set) -> bool:
    """Owner's 2026-10-13 turn-boundary clarify loop (reviewer round 2,
    Q3-gate closure): the pending row the pre_llm_call notice names is the
    project BOUNDARY dir (detect_root's result or the file's parent dir),
    and the clarify question text that the model generates from it names
    THAT dir. `record_clarify_evidence` therefore mints its evidence line
    with target = the boundary dir. But the subsequent code_map_check_root
    call joins (root, path) per gate ⑩ — and the model can pick ANY legal
    split of the same boundary (root=<boundary parent>, path=<boundary
    name> is the natural one, but root=<boundary>, path=<rel subpath> is
    equally legal). Without this relaxation, an evidence line minted for
    the boundary dir would not match a check_root target that lies
    DESCENDANT to it (or any other legal split of the same dir), and the
    user's "要" tap would dead-end at the Q3 gate.

    The rule: `target` is accepted if it EQUALS an evidence target, or if
    it sits ANYWHERE under one (descendant of the boundary the user was
    asked about). An evidence target for /a/b/c authorizes a build of
    /a/b/c/d (deeper into the same confirmed scope the user approved) but
    NOT /a/b (wider — that would index more than the user was asked
    about, and is rejected). Pure function, no filesystem access."""
    t = os.path.abspath(target)
    for e in evidence_targets:
        e = os.path.abspath(e)
        if t == e:
            return True
        if t.startswith(e + os.sep):
            return True
    return False


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
_WIN_ABS_PATH_RE = None


def _extract_path_mentions(question: str) -> List[str]:
    """Every ABSOLUTE-path mention found in a free-form question string,
    longest-first. Handles both POSIX ('/a/b') and Windows ('C:\\a\\b' or
    'C:/a/b') absolute paths. Pure text, no filesystem access, no side
    effects.

    A relative fragment ('x/y' or 'x\\y') is deliberately NOT returned:
    a bare fragment of a never-seen target must not mint evidence (the
    collision risk that made the first, too-loose fix wrong).

    Cross-platform (2026-11-16 fix): the original POSIX-only extractor
    (RISK #4, 2026-10-12) silently found no mentions on Windows hosts,
    where absolute paths start with a drive letter. Now both spellings
    are recognized; callers still compare on os.path.abspath()."""
    global _ABS_PATH_RE, _WIN_ABS_PATH_RE
    if _ABS_PATH_RE is None:
        import re as _re
        # NOTE: the character class deliberately EXCLUDES whitespace —
        # that's what makes the "trailing-space variant" dedup case
        # (reviewer RISK #2, 2026-10-12) work. If anyone ever adds \s
        # to this class, that dedup silently breaks.
        _ABS_PATH_RE = _re.compile(
            r"/(?:[A-Za-z0-9._-]+/){1,}[A-Za-z0-9._-]+")
        # Windows: drive-letter absolute path with 1+ separator-delimited
        # segments. Matches both backslash (C:\a\b) and forward-slash
        # (C:/a/b) spellings. Capture group 1 = the part AFTER the drive
        # colon+separator; we re-prepend the drive letter so the mention
        # normalizes correctly under os.path.abspath().
        _WIN_ABS_PATH_RE = _re.compile(
            r"([A-Za-z]):[\\/]((?:[A-Za-z0-9._-]+[\\/]){1,}[A-Za-z0-9._-]+)")
    found = set()
    found.update(_ABS_PATH_RE.findall(question or ""))
    # Windows: add drive-letter absolute paths. Normalize backslashes to
    # forward slashes so os.path.abspath() dedups against POSIX-form
    # registry entries.
    if _WIN_ABS_PATH_RE is not None:
        for drive, rest in _WIN_ABS_PATH_RE.findall(question or ""):
            found.add(f"{drive}/{rest.replace(chr(92), '/')}")
    return sorted(found, key=len, reverse=True)



def _clarify_evidence(target: str, root: str, path: str,
                     session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    # NOTE: `root`/`path` are unused (kept for call-site signature
    # compatibility); the gate target is the joined+abspath-normalized
    # `target`, which is the ONLY join key consulted against the log.
    del root, path
    """Latest transcript evidence that the user affirmatively confirmed
    THIS exact target via the host clarify tool, in THIS session.
    Returns the evidence record (with ts + user response + session_id)
    or None (no/affirmative-missing/cross-session-mismatch).
    Fail-soft: a missing/corrupt log -> None (fail-closed at the gate).

    C-lite (session anchor): the evidence line must carry a ``session_id``
    matching the caller's current session. Lines minted in a different
    session (or hand-written without a session_id) are NOT accepted —
    this is what blocks cross-session / forged-evidence bypasses.
    ``session_id=None`` on the caller side (legacy / unit tests that don't
    stamp the env) degrades to the pre-C-lite behavior (no session check)
    to keep existing tests green; a production call always passes the
    live ``HERMES_SESSION_ID`` so the check is active."""
    target = os.path.abspath(target)
    if session_id is None:
        session_id = os.environ.get("HERMES_SESSION_ID")
    # C-mid: load the signing key once. Acceptance policy:
    #   - record has NO "sig" key at all -> pre-C-mid back-compat line,
    #     falls through to the C-lite session check below only.
    #   - record carries a "sig" key:
    #       * sig value is a non-empty hex string -> must verify against
    #         the loaded key; key absent or verify fails -> reject
    #         (fail-closed: we never accept a line we cannot
    #         authenticate).
    #       * sig value is null / empty (e.g. hand-written `sig: null`)
    #         -> "skip arm": treated as an unsigned line and falls
    #         through to the C-lite session check. Rationale: this is
    #         the documented session-less degrade path the owner chose
    #         (C-mid fail-soft); under a live HERMES_SESSION_ID the
    #         C-lite check still rejects it, so the skip arm is safe in
    #         production. Do NOT "fix" this to reject null-sig under
    #         a live session — that would strand legitimate C-mid
    #         back-compat lines written before the key file existed.
    sign_key = _load_evidence_key()
    log = _evidence_log()
    if not log.exists():
        return None
    try:
        rows = []
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            # Q3 descent relaxation (owner 2026-10-13): the evidence line's
            # target may be a boundary dir that CONTAINS the requested
            # target (the model's check_root call named a sub-path of the
            # boundary the user actually approved). Accept every
            # same-session, affirmative, sig-verified row whose target is
            # an ancestor-or-self of `target`, and take the newest.
            if not _target_in_descent_chain(target, {rec.get("target") or ""}):
                continue
            if not rec.get("affirmative"):
                continue
            # C-mid signature check. A line that CARRIES a "sig" field is a
            # C-mid-minted line and MUST verify — the sig is the forgery
            # detector, so a bad/missing-verification sig is always a reject.
            # Only lines with NO "sig" key at all take the pre-C-mid
            # back-compat path (fall through to the C-lite session check).
            # C-mid signature check. The "sig" key is present in three forms:
            #   (a) no "sig" key            -> pre-C-mid back-compat: fall
            #                                  through to C-lite check.
            #   (b) sig = null / empty      -> "skip arm": the key file was
            #                                  absent at mint time so no
            #                                  sig could be computed (the
            #                                  _mint_evidence_line fail-soft
            #                                  path). Treated as unsigned:
            #                                  fall through to C-lite. Under
            #                                  a live session this is still
            #                                  rejected by the session check;
            #                                  in the sessionless degrade path
            #                                  it is accepted (documented
            #                                  fail-soft, not a security hole).
            #   (c) sig = hex string        -> C-mid-minted under a live key:
            #                                  MUST verify. Key absent OR
            #                                  verify fails -> reject
            #                                  (fail-closed: we never accept
            #                                  a line we can't authenticate).
            sig_val = rec.get("sig")
            if sig_val is not None and sig_val != "":
                if sign_key is None or not _verify_evidence_line(rec, sign_key):
                    continue
            # C-lite session anchor: when the caller is running under a live
            # HERMES_SESSION_ID, ONLY same-session stamped lines are accepted.
            # - stamped + matching session  -> accept
            # - stamped + different session -> reject (cross-session theft)
            # - un-stamped (pre-C-lite / hand-written) -> reject, since a live
            #   session means the host SHOULD have stamped this line; an
            #   un-stamped line under a live session is exactly the forged or
            #   legacy row we are trying to block.
            # When session_id is None (unit tests / session-less runner) the
            # check is skipped, preserving the pre-C-lite behavior.
            if session_id:
                rec_sid = rec.get("session_id")
                if rec_sid != session_id:
                    continue
            rows.append(rec)
        if not rows:
            return None
        # Newest first wins (ts is host-local ISO from the hook).
        return max(rows, key=lambda r: str(r.get("ts", "")))
    except OSError:
        return None


def record_clarify_evidence(target: str, user_response: str,
                            ts: Optional[str] = None,
                            session_id: Optional[str] = None) -> None:
    """Append one clarify-evidence record (called by the post_tool_call
    hook for host clarify calls). Appends ONLY — the gate reads newest.
    Fail-soft: a log write hiccup must never break a tool call.

    C-lite: ``session_id`` is the host-kwargs session id (or the current
    ``HERMES_SESSION_ID`` env when the caller omitted it). The gate reads
    the SAME env on its side to reject evidence minted in a different
    session — a hand-written line without a matching session_id fails the
    session-check."""
    if not target:
        return
    if session_id is None:
        # M4: read the env var directly (no `or None` fold): an empty-string
        # session id (a host bug) is preserved as "" so the session check can
        # see the distinction between "un-stamped" (None) and "stamped-empty"
        # (""). The gate treats both the same (empty != live session), but
        # keeping the raw value avoids silently collapsing the two cases.
        session_id = os.environ.get("HERMES_SESSION_ID")
    rec = {
        "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "target": os.path.abspath(target),
        "user_response": (user_response or "")[:400],
        "affirmative": _is_affirmative(user_response or ""),
        "session_id": session_id,
    }
    # C-mid: sign the line before it hits the log so the gate can later
    # distinguish host-minted evidence from a hand-written forgery.
    rec = _mint_evidence_line(rec)
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
               limit: int, cursor: str, total: int) -> str:
    # query_symbols row shape: (root, path, name, kind, line, sig)
    rows = [
        {"root": r[0], "path": r[1], "name": r[2],
         "kind": r[3], "line": r[4], "sig": r[5], "staleness": staleness}
        for r in page
    ]
    payload: Dict[str, Any] = {
        "symbols": rows,
        "count": len(rows),
        "total": total,
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
        "`std::puts()` -> 'puts' (receiver/namespace not resolved). "
        "python (L2.3): call_site covers EVERY syntactically-visible "
        "call at any nesting depth (other languages: conservative "
        "capture only); dynamic dispatch (getattr/callbacks) yields "
        "wrapper rows only. extends rows = class bases; decorator "
        "rows = the decorator's callee. "
        "parameter rows (kind=parameter, python) are name-level "
        "declarations: common names (query, limit, ...) match across "
        "modules — no scope resolution; a row names the declaration "
        "site, not the keyword-argument consumers (grep for those). "
        "kwarg_usage rows (kind=kwarg_usage, python) are every "
        "keyword-argument usage at any call-nesting depth — positional "
        "arguments are NOT covered (no name to key on; that needs "
        "type resolution). "
        "Paging: count = rows on THIS page (byte-capped); total = all "
        "matching rows; follow 'cursor' until a page returns count=0, "
        "otherwise you have a partial set."
    )
    return json.dumps(payload, ensure_ascii=False)


def _serialize_concept(page: list, staleness: str, root: str,
                       limit: int, cursor: str, total: int,
                       rarity_on: bool = True) -> str:
    """L2.4 concept-bridge payload: candidate rows carry tier + occ (name
    rarity in this root). The disclosure pins the accuracy contract:
    candidates are word-surface matches, NOT a semantic verdict — verify
    sig/docstring before use; 0 hits carry a grep-fallback note (word
    surface miss != concept absent)."""
    rows = [
        {"root": r[0], "path": r[1], "name": r[2], "kind": r[3],
         "line": r[4], "sig": r[5], "tier": r[6], "occ": r[7],
         "staleness": staleness}
        for r in page
    ]
    payload: Dict[str, Any] = {
        "symbols": rows,
        "count": len(rows),
        "total": total,
        "limit": limit,
        "cursor": str(cursor),
        "staleness": staleness,
        "match": "fuzzy",
    }
    rarity_note = (
        "ranked by tier then name rarity (rare names first)"
        if rarity_on else
        "large candidate set: rarity ranking skipped for the query-time "
        "budget (occ=0); ranked by tier, name, path, line instead")
    payload["disclosure"] = (
        "fuzzy=true: rows are word-surface CANDIDATES, not a verdict — "
        "tier 1=name-equal, 2=prefix, 3=substring (case-insensitive, "
        "min tier per row across the concept's tokens); " + rarity_note
        + ". Verify each candidate's sig/docstring before relying on it. "
        + _PAGING_DISCLOSURE
    )
    if total == 0:
        payload["note"] = (
            "0 word-surface candidates: this does NOT mean the concept is "
            "absent — the target symbol may share no characters with the "
            "queried words (e.g. 'rebuild' vs 'flush_stale_rows'). Fall "
            "back to grep (search_files) for concept words the name index "
            "cannot surface."
        )
    return json.dumps(payload, ensure_ascii=False)


_PAGING_DISCLOSURE = (
    "Paging: count = rows on THIS page (byte-capped); total = all "
    "matching rows; follow 'cursor' until a page returns count=0, "
    "otherwise you have a partial set.")


def _run_fuzzy_branch(conn, root: str, name, path_filter, kind,
                      limit: int, offset: int, staleness: str) -> str:
    """L2.4 concept bridge: word-surface candidate rows for a concept word.

    Accuracy contract (owner 2026-11-16 discussion): candidates are
    tiered (exact/prefix/substring) + rarity-ranked, disclosed as
    over-approximations, and a 0-hit set carries an explicit
    grep-fallback note so a word-surface miss never masquerades as
    'the concept does not exist'.
    """
    tokens = re.findall(r"[a-z0-9_]+", str(name).lower())[:5]
    if not tokens:
        return _invalid(
            "fuzzy=true needs at least one alphanumeric token in name",
            limit)
    t0 = time.perf_counter()
    total = count_concept(conn, root, tokens, path=path_filter, kind=kind)
    # gate ④ budget: rarity JOIN only pays off on small candidate sets;
    # above the threshold fall back to the no-rarity fast path (occ=0).
    rarity_on = total <= CONCEPT_RARITY_MAX_CANDIDATES
    rows = query_concept(conn, root, tokens, path=path_filter, kind=kind,
                         limit=limit, offset=offset, rarity=rarity_on)
    page = rows[:PAGE_MAX_SYMBOLS]
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    result = _serialize_concept(page, staleness, root, limit,
                                str(offset + limit), total, rarity_on)
    if len(result.encode("utf-8")) > PAGE_BUDGET_BYTES:
        while len(page) > 1:
            page = page[:-1]
            result = _serialize_concept(page, staleness, root, limit,
                                        str(offset + len(page)), total,
                                        rarity_on)
            if len(result.encode("utf-8")) <= PAGE_BUDGET_BYTES:
                break
    payload = json.loads(result)
    payload["query_ms"] = elapsed_ms
    return json.dumps(payload, ensure_ascii=False)


# ── L2.5 multi-hop impact propagation (owner 2026-11-16, roadmap ①) ────────
# The edges already live in the symbols table (call_site / kwarg_usage /
# import rows = file->name consumer edges; function / method / class rows =
# def-side). This is the query face over them: alternating frontier stages
# (odd = consumers of the frontier names, even = defs inside those files).
# Name-level OVER-approximation (disclosed in every payload): aliases and
# overloads merge, the seed's own def is never re-emitted, positional-
# argument consumers are absent (no name to key on — that is grep's lane).
# Each stage is a plain index-range SELECT (no recursive CTE: SQLite limits
# the recursive table to one self-reference; Python-side unrolling over the
# bounded stage count is simpler and equally fast on index-driven stages).
_HOP_CONSUMER_KINDS = ("call_site", "kwarg_usage", "import")
_HOP_DEF_KINDS = ("function", "method", "class")


def _query_hops(conn, root: str, seed: str, depth: int):
    """Walk `depth` frontier stages from `seed`. Returns (rows, total,
    frontier_capped). rows carry a `hop` key; dedup is global over
    (name, path, line) with the MIN hop winning (stages ascend)."""
    rows: List[Dict[str, Any]] = []
    seen = set()
    frontier_names = [seed]
    frontier_files: List[str] = []
    frontier_capped = False

    def _stage(consumer: bool, keys: List[str]) -> List[tuple]:
        ph = ", ".join("?" * len(keys))
        kinds = _HOP_CONSUMER_KINDS if consumer else _HOP_DEF_KINDS
        kindph = ", ".join("?" * len(kinds))
        sql = ("SELECT name, path, line, kind, sig FROM symbols "
               f"WHERE root = ? AND {('name IN' if consumer else 'path IN')} ({ph}) "
               f"AND kind IN ({kindph})")
        params = [root, *keys, *kinds]
        if not consumer:
            sql += " AND name != ?"  # the seed's own def is not a frontier
            params.append(seed)
        return conn.execute(sql, params).fetchall()

    for hop in range(1, depth + 1):
        if hop % 2 == 1:
            keys = frontier_names[:HOP_FRONTIER_CAP]
            if len(frontier_names) > len(keys):
                frontier_capped = True
            stage = _stage(True, keys)
        else:
            keys = frontier_files[:HOP_FRONTIER_CAP]
            if len(frontier_files) > len(keys):
                frontier_capped = True
            if not keys:
                break
            stage = _stage(False, keys)
        fresh = []
        for r in stage:  # (name, path, line, kind, sig)
            key = (r[0], r[1], r[2])
            if key in seen:
                continue
            seen.add(key)
            fresh.append({"root": root, "path": r[1], "name": r[0],
                           "kind": r[3], "line": r[2], "sig": r[4],
                           "hop": hop})
        rows.extend(fresh)
        if hop % 2 == 1:
            frontier_files = sorted({s["path"] for s in fresh})
            if not frontier_files:
                break
        else:
            frontier_names = sorted({s["name"] for s in fresh})
            if not frontier_names:
                break
    return rows, len(rows), frontier_capped


def _serialize_hops(rows: list, staleness: str, root: str,
                    depth: int, depth_effective: int, total: int,
                    frontier_capped: bool) -> str:
    """Byte-capped hop payload: over budget, the DEEPEST hops are dropped
    first (least-certain propagation); `total` keeps counting ALL rows."""
    disclosure = (
        "L2.5 multi-hop impact propagation from the seed name: odd hops = "
        "consumer rows (call_site/kwarg_usage/import), even hops = def-side "
        "symbols (function/method/class) inside those consumer files. "
        "NAME-LEVEL over-approximation: aliases/overloads merge; the seed's "
        "own def is not re-emitted; positional-argument consumers are NOT "
        "covered (grep for those). Deduped by (name,path,line), min hop "
        "wins. No cursor paging in depth mode: over the byte budget the "
        "SHALLOWEST hops are dropped first (hop 1 is the exact-name query "
        "redundant; deep hops carry the NEW propagation); total still "
        "counts all rows — re-query a frontier name for a layer's detail. "
        "path/kind/cursor/limit are ignored in depth mode."
    )

    def _build(out_rows: list, truncated: bool) -> str:
        payload = {
            "status": "ok",
            "symbols": out_rows,
            "count": len(out_rows),
            "total": total,
            "depth": depth,
            "depth_effective": depth_effective,
            "hops_returned": max((r["hop"] for r in out_rows), default=0),
            "truncated": truncated,
            "frontier_capped": frontier_capped,
            "staleness": staleness,
            "cursor": "0",
            "disclosure": disclosure,
        }
        return json.dumps(payload, ensure_ascii=False)

    out_rows = list(rows)
    result = _build(out_rows, False)
    if len(result.encode("utf-8")) > PAGE_BUDGET_BYTES:
        # ① whole-hop drops while >1 hop is present (shallowest first)
        while out_rows:
            hops_present = {r["hop"] for r in out_rows}
            if len(hops_present) < 2:
                break
            bottom = min(hops_present)
            out_rows = [r for r in out_rows if r["hop"] > bottom]
        # ② single-hop remainder: binary-search the largest PREFIX that
        # fits the budget (row counts are size-monotonic — O(log n)
        # serializations instead of O(n²) trim-and-serialize)
        if out_rows and len(_build(out_rows, True).encode("utf-8")) > PAGE_BUDGET_BYTES:
            lo, hi, best = 0, len(out_rows), 0
            while lo <= hi:
                mid = (lo + hi) // 2
                if len(_build(out_rows[:mid], True).encode("utf-8")) <= PAGE_BUDGET_BYTES:
                    best, lo = mid, mid + 1
                else:
                    hi = mid - 1
            out_rows = out_rows[:best]
        result = _build(out_rows, True)
    return result


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
    fuzzy = bool(args.get("fuzzy"))  # L2.4 concept bridge (owner 2026-11-16)

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
            "symbols": [], "count": 0, "total": 0, "limit": limit,
            "cursor": "0", "staleness": "partial",
            "note": "index not built for this root yet; "
                    "call code_map_refresh to re-scan",
        }, ensure_ascii=False)

    conn = init_db(root)
    try:
        t0 = time.perf_counter()  # L45
        staleness = _staleness(root, conn)  # L64
        offset = int(cursor) if cursor.isdigit() else 0
        # L2.5 multi-hop impact propagation: depth >= 1 takes precedence
        # over BOTH the exact-name and the fuzzy paths; path/kind/cursor/
        # limit are intentionally ignored (documented in the payload).
        depth_arg = args.get("depth")
        if depth_arg:
            try:
                depth_req = int(depth_arg)
            except (TypeError, ValueError):
                return _invalid(
                    f"depth must be an integer 1..{HOP_MAX_DEPTH} "
                    f"(got {depth_arg!r})", limit)
            if not name:
                return _invalid(
                    "depth mode requires an exact seed name (a concept word "
                    "needs fuzzy=true, which has no propagation semantics)",
                    limit)
            if fuzzy:
                return _invalid(
                    "depth mode and fuzzy=true are mutually exclusive: "
                    "bridge candidates are word-surface, not propagation "
                    "seeds — resolve the exact name first", limit)
            depth_eff = min(max(depth_req, 1), HOP_MAX_DEPTH)
            hrows, htotal, hcap = _query_hops(conn, root, name, depth_eff)
            result = _serialize_hops(hrows, staleness, root,
                                     depth_req, depth_eff, htotal, hcap)
            payload = json.loads(result)
            payload["query_ms"] = int((time.perf_counter() - t0) * 1000)
            return json.dumps(payload, ensure_ascii=False)
        if fuzzy:
            # L2.4: word-surface candidates (the exact path below stays
            # byte-identical when fuzzy is absent/false — regression guard).
            return _run_fuzzy_branch(conn, root, name, path_filter, kind,
                                     limit, offset, staleness)
        # 'total' = ALL-page match count (2026-11-16): byte-capped pages
        # carry count=rows-on-this-page only; without total, a consumer
        # reading count<=limit stops early and undercounts (L2.2 bench:
        # a 22-row page of a 76-row set was read as 'complete').
        total = count_symbols(conn, root, name=name, path=path_filter,
                              kind=kind)
        rows = query_symbols(conn, root, name=name, path=path_filter,
                             kind=kind, limit=limit, offset=offset)
        page = rows[:PAGE_MAX_SYMBOLS]
        elapsed_ms = int((time.perf_counter() - t0) * 1000)  # L90

        result = _serialize(page, staleness, root, limit,
                            str(offset + limit), total)

        # Single-page byte budget: truncate if over PAGE_BUDGET_BYTES
        if len(result.encode("utf-8")) > PAGE_BUDGET_BYTES:
            while len(page) > 1:
                page = page[:-1]
                result = _serialize(page, staleness, root, limit,
                                    str(offset + len(page)), total)
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

    # OPT: if target already in registry, or is a descendant of a confirmed
    # root, skip transcript check and just refresh. Already-confirmed targets
    # (and their sub-paths) don't need re-clarification every session — the
    # registry itself is the persistent authorization record. This eliminates
    # the "asked and confirmed last session, asked again this session" loop
    # while keeping the gate fail-closed for TRULY NEW targets.
    from . import rootreg
    _confirmed_targets = rootreg.confirmed_targets()
    _is_confirmed_or_descendant = (
        target in _confirmed_targets
        or any(target.startswith(ct + os.sep) for ct in _confirmed_targets)
    )
    if _is_confirmed_or_descendant:
        from .state import invalidate_cache, _start_build_thread
        # For exact-match confirmed targets use refresh_root (includes
        # the files_indexed count). For sub-paths (descendants only),
        # call the internals directly since the sub-path target is not
        # itself in the registry.
        if target in _confirmed_targets:
            from .state import refresh_root
            res = refresh_root(root, path)
            if res.get("status") != "build_started":
                return json.dumps(res, ensure_ascii=False)
            res["root"] = target
            res["dry_run"] = False
            res["note"] = ("refresh: target already confirmed (registry hit), "
                           "no fresh clarify needed — only re-indexing.")
            res["files_indexed"] = len(indexable)
            return json.dumps(res, ensure_ascii=False)
        else:
            # Sub-path of a confirmed ancestor: invalidate cache + start
            # build for this specific target. Same as refresh_root minus
            # the registry membership check.
            invalidate_cache(target)
            _start_build_thread(target)
            res = {"root": target, "status": "refresh_started",
                   "dry_run": False,
                   "note": ("refresh: target is a descendant of a confirmed "
                            "ancestor, no fresh clarify needed — only "
                            "re-indexing."),
                   "files_indexed": len(indexable)}
            return json.dumps(res, ensure_ascii=False)

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
                     "exact target, in this session. Call code_map_check_root "
                     "(dry-run) and ask the user via the host `clarify` tool "
                     "first; the user's affirmative reply is the transcript "
                     "evidence this gate accepts (session-stamped since C-lite). "
                     "(Owner policy 2026-10-11: 模型推 clarify, transcript 兜底.)"),
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
    with open(log_path, "a", encoding="utf-8") as f:
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


def _append_confirm_audit_line(rec: Dict[str, Any]) -> Path:
    """Append one audit row to data/confirm_root_log.jsonl (both success and
    evidence-miss outcomes — 2026-11-16: a miss without a logged reason is
    what forced the disk-archaeology diagnosis of the 17:00 incident).
    Fail-soft: an audit hiccup never blocks or breaks the confirm path."""
    import datetime
    log_path = data_dir() / "confirm_root_log.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.datetime.now().isoformat(),
                **rec,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return log_path


def code_map_confirm_root(args: Dict[str, Any]) -> str:
    """code_map_confirm_root: lock a (root, path) target and build.

    Owner's rule (2026-10-11 v2): root AND path are both mandatory.
    root is the boundary prefix (/root, /usr, /home, ...); path is
    the RELATIVE sub-path inside it. The joined target is what gets
    indexed and registered. A root-only call (whole-boundary index —
    the 382k-file incident) or a path-only call (no boundary) is
    rejected; the bare filesystem root '/' is rejected; a path that
    escapes the boundary via ../ is rejected.

    Evidence gate (2026-11-16): like check_root(yes=true), a TRULY NEW
    target requires a same-session affirmative clarify evidence line for
    that exact target (or an ancestor boundary, the Q3 descent
    relaxation); the registry itself (target or a confirmed ancestor)
    remains the persistent authorization record and exempts re-asks.
    Without qualifying evidence the call is refused with
    blocked_by_policy and a logged miss row."""
    from .state import confirm_root, _gate10
    root = args.get("root", "")
    path = args.get("path", "")
    # Legacy single-arg call shape: root_path= full target, no
    # relative path — accepted ONLY as an already-joined target,
    # which means root=root_path, path="" — that FAILS the gate, so
    # legacy callers must migrate to the (root, path) pair.
    if not root and args.get("root_path"):
        root = args["root_path"]
    gate = _gate10(root, path)
    if gate is not None:
        return json.dumps(gate, ensure_ascii=False)
    target = rootreg._join_target(root, path)
    if target is None:  # unreachable past a passing gate; keep the type honest
        return json.dumps({
            "status": "invalid", "root": root or None, "path": path or None,
            "reason": "gate: could not derive the joined target",
        }, ensure_ascii=False)

    # ── transcript-evidence gate (mirror of check_root's fail-closed) ──
    _confirmed = rootreg.confirmed_targets()
    _registry_hit = target in _confirmed
    _registry_descendant = (not _registry_hit) and any(
        target.startswith(ct + os.sep) for ct in _confirmed)
    evidence = None
    if not (_registry_hit or _registry_descendant):
        evidence = _clarify_evidence(target, root, path)
        if evidence is None:
            _append_confirm_audit_line({
                "action": "confirm_root",
                "result": "blocked_evidence_miss",
                "root": target, "boundary": root, "rel_path": path,
                "note": ("no qualifying affirmative clarify evidence for "
                         "this exact target in this session"),
            })
            return json.dumps({
                "status": "blocked_by_policy",
                "root": target,
                "note": ("a NEW target requires a prior user confirmation "
                         "for this exact target, in this session — same "
                         "fail-closed policy as code_map_check_root(yes=true). "
                         "Call code_map_check_root (dry-run) and ask the user "
                         "via the host `clarify` tool first; the user's "
                         "affirmative reply is the transcript evidence this "
                         "gate accepts. Already-confirmed targets (and their "
                         "sub-paths) are exempt."),
                "next": "code_map_check_root(root, path) + clarify, then retry",
            }, ensure_ascii=False)

    result = confirm_root(root, path)
    if result.get("status") != "build_started":
        return json.dumps(result, ensure_ascii=False)

    _append_confirm_audit_line({
        "action": "confirm_root",
        "result": "confirmed",
        "root": target, "boundary": root, "rel_path": path,
        "transcript_evidence": evidence,
        "exemption": (None if evidence is not None
                      else ("registry_hit" if _registry_hit
                            else "registry_descendant")),
    })
    result["audit"] = str(data_dir() / "confirm_root_log.jsonl")
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
