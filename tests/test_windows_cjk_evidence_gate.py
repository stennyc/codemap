"""Windows CJK evidence-gate regression (2026-11-16 incident).

The confirm gate reads data/_clarify_evidence.jsonl, which carries the
user's raw clarify replies. On this host the reply is CJK ("要：index
...（127 个文件）...") so the evidence line holds UTF-8 multi-byte
bytes. The host process' default locale codec is NOT utf-8 (Windows
cp1252/'charmap'), so the bare ``Path.read_text()`` in
``query._clarify_evidence`` crashed code_map_check_root(yes=True) with
``UnicodeDecodeError: 'charmap' codec can't decode byte 0x81`` — a hard
TOOL_ERROR instead of the gate's documented fail-soft.

Fix under test: every text I/O on the gate/registry path pins
``encoding="utf-8"`` (reads add ``errors="replace"`` for fail-soft).
"""
import inspect
import os
import sys
from pathlib import Path

import pytest

# 自适应 bootstrap（与本目录其他 test_*.py 同一约定）: 插件根进 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from code_map import query as q  # noqa: E402


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Point data_dir() at a tmp tree and give the gate a live session."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-cjk-test")
    return tmp_path


def test_cjk_reply_round_trips_through_evidence_gate(isolated_home):
    """Incident shape: an affirmative CJK reply for a target, minted by
    the real writer, must be read back by the gate with content intact.
    Pre-fix the gate's read used the host's default locale codec and
    raised UnicodeDecodeError on the CJK bytes (0x81 = third byte of
    '要' in UTF-8 — the exact byte in the crash report)."""
    target = os.path.abspath(str(isolated_home / "plugins" / "filemem"))
    cjk_reply = "要：index " + target + "（127 个文件）并跑对照测试"
    q.record_clarify_evidence(target, cjk_reply, session_id="sess-cjk-test")

    log = q._evidence_log()
    assert log.exists()
    # Precondition of the bug: CJK bytes really are on disk (UTF-8).
    assert "要".encode("utf-8") in log.read_bytes()

    rec = q._clarify_evidence(target, None, None, session_id="sess-cjk-test")
    assert rec is not None, "gate lost the affirmative CJK evidence"
    # The reply must survive the gate's read without mojibake. Compare the
    # PARSED record value (a JSON-escaped dumps would false-negative on
    # Windows backslashes).
    assert rec.get("user_response") == cjk_reply
    assert rec.get("affirmative") is True


def test_evidence_gate_read_is_encoding_pinned():
    """Source-level guard: the gate's log read must pin utf-8 explicitly.
    A bare read_text() is host-locale dependent (fine on a UTF-8 Linux
    box, crashes on a Windows cp1252 host) — the incident was exactly
    that, and only this assertion would have caught it in CI."""
    src = inspect.getsource(q._clarify_evidence)
    assert "read_text(encoding=\"utf-8\"" in src, (
        "evidence gate must read the log with an explicit utf-8 "
        "encoding (Windows charmap incident, 2026-11-16)")


def test_no_bare_read_text_in_main_modules():
    """Sweep: no argument-less read_text() may remain in the plugin's
    main modules. Registry / evidence / dirty / ledger files carry
    user-supplied paths and replies — CJK on a Chinese-locale user,
    any non-ASCII on a Western-locale host. An unpinned read is
    host-locale dependent and will crash the same way again."""
    pkg_dir = Path(q.__file__).resolve().parent
    offenders = []
    for mod in ("__init__.py", "query.py", "rootreg.py", "hook.py",
                "state.py", "scanner.py", "fingerprint.py",
                "registry.py", "db.py", "parser.py", "prune.py",
                "rootdetect.py"):
        p = pkg_dir / mod
        if not p.exists():
            continue
        for i, line in enumerate(
                p.read_text(encoding="utf-8").splitlines(), 1):
            if "read_text()" in line:
                offenders.append(f"{mod}:{i}: {line.strip()}")
    assert not offenders, (
        "unpinned read_text() reintroduces the host-locale crash: "
        f"{offenders}")
