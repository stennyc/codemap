"""hook.py — Tier-1 transform_tool_result hook (fail-soft, gate ⑨).

Fires on every write_file/patch result. If the target file is under a
confirmed root, schedule a background re-parse of that single file
(debounced HOOK_DEBOUNCE_S). No-op for non-confirmed roots or parse failures.
Go/Rust method disclosure lives here (L157 in the sealed version).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import HOOK_DEBOUNCE_S, data_dir
from .rootdetect import detect_root

_log = logging.getLogger("code_map.hook")

# Debounce: last-schedule time per root, to coalesce burst writes.
_last_schedule: Dict[str, float] = {}


def on_write_result(tool_result: Dict[str, Any]) -> Optional[str]:
    """transform_tool_result hook handler. Returns None (no-op) in most cases.

    Fires after write_file/patch. Fail-soft: any exception -> None, never
    blocks the tool result from returning to the LLM (gate ⑨).
    """
    try:
        return _maybe_reindex(tool_result)
    except Exception as exc:
        _log.debug("code_map hook fail-soft: %s", exc)
        return None


def _maybe_reindex(tool_result: Dict[str, Any]) -> Optional[str]:
    """Check if the written file is under a confirmed root; schedule reindex."""
    # Only act on write_file / patch tool results
    tool_name = tool_result.get("_tool", "")
    if tool_name not in ("write_file", "patch"):
        return None

    file_path = tool_result.get("path") or tool_result.get("target")
    if not file_path:
        return None
    file_path = str(file_path)

    # Must be under a confirmed root
    confirmed_roots = _confirmed_roots()
    root = _find_confirmed_root(file_path, confirmed_roots)
    if root is None:
        return None  # not under a confirmed root -> no-op (gate ⑨)

    # Debounce
    now = time.time()
    last = _last_schedule.get(root, 0.0)
    if now - last < HOOK_DEBOUNCE_S:
        return None  # already scheduled for this burst
    _last_schedule[root] = now

    # Schedule a single-file re-parse in the background
    _schedule_single_file_reindex(root, file_path)
    return None


def _confirmed_roots() -> set:
    """Read the set of confirmed roots from the registry file."""
    reg = data_dir() / "_registry"
    if not reg.exists():
        return set()
    try:
        with open(reg) as f:
            return {line.strip() for line in f if line.strip()}
    except OSError:
        return set()


def _find_confirmed_root(file_path: str, confirmed: set) -> Optional[str]:
    """Return the confirmed root that `file_path` falls under (nearest)."""
    abspath = os.path.abspath(file_path)
    best = None
    for root in confirmed:
        root_abs = os.path.abspath(root)
        if abspath.startswith(root_abs + os.sep) or abspath == root_abs:
            if best is None or len(root_abs) > len(best):
                best = root_abs
    return best


def _schedule_single_file_reindex(root: str, file_path: str) -> None:
    """Parse one file and upsert it, in a background thread (non-blocking)."""
    from .db import init_db, insert_symbols, upsert_file
    from .fingerprint import file_fingerprint, invalidate_cache
    from .parser import parse_file
    from .config import BUILD_COMMIT_BATCH

    def _work():
        conn = init_db(root)
        try:
            rows = parse_file(file_path)  # fail-soft: [] on parse error
            p = Path(file_path)
            mtime, size = file_fingerprint(p)
            ext = p.suffix.lower()
            lang_map = {".py": "python", ".ts": "typescript", ".tsx": "typescript",
                        ".js": "javascript", ".jsx": "javascript", ".go": "go",
                        ".rs": "rust", ".java": "java", ".dart": "dart",
                        ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp"}
            lang = lang_map.get(ext, "unknown")
            upsert_file(conn, file_path, mtime, size, lang,
                        "hook", commit=False)
            insert_symbols(conn, root, file_path, rows, commit=False)
            conn.commit()
            invalidate_cache(root)
        except Exception as exc:
            _log.debug("code_map single-file reindex fail: %s", exc)
        finally:
            conn.close()

    t = threading.Thread(target=_work, daemon=True)
    t.start()
