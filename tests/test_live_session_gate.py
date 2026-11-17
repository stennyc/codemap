"""Live-session heartbeat for the C-lite evidence gate (2026-11-17).

Live failure this fixes: kbs confirm — the evidence line was minted with
the LIVE CHAT session id (post_tool_call kwargs), but the gate caller
reads HERMES_SESSION_ID from the GATEWAY PROCESS env (a different stamp,
set at gateway startup) -> C-lite session-anchor mismatch ->
blocked_by_policy despite three unambiguous owner affirmatives.

Design (plugin-only, no host edits — the host passes no session kwargs
to plugin tool handlers, verified at plugins_context.py:335 which calls
the handler with dict(arguments) only):
  - WRITE side: hook._touch_live_session refreshes a heartbeat file
    (data/_live_session.json = {"session_id", "ts"}) on every post_tool_call
    that carries a session kwarg — the hook face is the ONLY surface that
    sees the per-call live session.
  - READ side: query._clarify_evidence accepts an evidence line whose
    session stamp equals ANY live identity known to the gate:
    {explicit param, process env, heartbeat (fresh only, <=10min)}.
  - Fail-closed unchanged: with NO live identity known the session check
    degrades to the pre-C-lite behavior; a STALE heartbeat is ignored
    (the live session has moved on); an unstamped line is still rejected
    while any live identity is known.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest  # noqa: E402

from code_map import hook, query  # noqa: E402

LIVE_SESSION_FILE = "_live_session.json"
LIVE = "local-live-chat-session"
OTHER = "local-gateway-startup-stamp"


def _setup(tmp_path, monkeypatch, tag):
    home = tmp_path / f"home_{tag}"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    return home


def _data_dir():
    from code_map.config import data_dir
    return data_dir()


def _mint(target, stamp):
    """Mint an evidence line the way the host-minted shape looks.

    stamp=None + no env -> the line carries session_id=None (the
    'un-stamped' case the C-lite check distinguishes from stamped rows).
    record_clarify_evidence returns None by contract (append-only log)."""
    if stamp is None:
        os.environ.pop("HERMES_SESSION_ID", None)
    else:
        os.environ["HERMES_SESSION_ID"] = stamp
    query.record_clarify_evidence(target, "要", session_id=stamp)
    return _data_dir().parent / "_clarify_evidence.jsonl"


def _heartbeat(session_id, age_s=0.0):
    import datetime
    ts = (datetime.datetime.now() -
          datetime.timedelta(seconds=age_s)).isoformat()
    p = _data_dir() / LIVE_SESSION_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"session_id": session_id, "ts": ts}))
    return p


def _gate(target, env_session):
    if env_session is None:
        os.environ.pop("HERMES_SESSION_ID", None)
    else:
        os.environ["HERMES_SESSION_ID"] = env_session
    return query._clarify_evidence(target, "", "")


def test_fresh_heartbeat_accepts_live_chat_evidence(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "hb")
    t = str(tmp_path / "proj")
    _mint(t, LIVE)
    _heartbeat(LIVE)
    # gate caller only knows the OTHER (gateway-env) identity:
    assert _gate(t, OTHER) is not None


def test_stale_heartbeat_is_ignored(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "stale")
    t = str(tmp_path / "proj")
    _mint(t, LIVE)
    _heartbeat(LIVE, age_s=999999)
    assert _gate(t, OTHER) is None


def test_no_heartbeat_falls_back_to_env_identity(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "env")
    t = str(tmp_path / "proj")
    _mint(t, OTHER)
    assert _gate(t, OTHER) is not None
    assert _gate(t, LIVE) is None


def test_unstamped_line_rejected_while_live_identity_known(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "unstamp")
    t = str(tmp_path / "proj")
    _mint(t, None)          # un-stamped line
    _heartbeat(LIVE)        # a live identity IS known (heartbeat)
    assert _gate(t, OTHER) is None


def test_no_live_identity_at_all_degrades_to_legacy(tmp_path, monkeypatch):
    """No heartbeat, no env: pre-C-lite face — a stamped LIVE line is
    still found (back-compat, keeps sessionless runners green)."""
    _setup(tmp_path, monkeypatch, "legacy")
    t = str(tmp_path / "proj")
    _mint(t, LIVE)
    assert _gate(t, None) is not None


def test_hook_touch_writes_heartbeat(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "touch")
    hook._touch_live_session(LIVE)
    p = _data_dir() / LIVE_SESSION_FILE
    assert p.exists()
    d = json.loads(p.read_text())
    assert d["session_id"] == LIVE and "ts" in d
    # empty/None session must not clobber a good heartbeat
    hook._touch_live_session(None)
    hook._touch_live_session("")
    assert json.loads(p.read_text())["session_id"] == LIVE
