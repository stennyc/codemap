"""query.py — code_map_query tool handler (L1.5 cross-file symbol location).

Gate ④ timing window: L45 t0 → L64 _staleness → L90 elapsed_ms (includes B+
diff cost). Disclosure line L113: Go/Rust method names are unqualified +
Rust methods surface as kind=function.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from .config import PAGE_BUDGET_BYTES, PAGE_MAX_SYMBOLS, root_db_path
from .db import init_db, query_symbols, stale_paths
from .fingerprint import diff_indexed, invalidate_cache, on_disk_files


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
    rows = [
        {"root": root, "path": r[0], "name": r[1],
         "kind": r[2], "line": r[3], "sig": r[4], "staleness": staleness}
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
    payload["disclosure"] = (
        "Go/Rust: method names are unqualified; Rust methods "
        "surface as kind=function."
    )
    return json.dumps(payload, ensure_ascii=False)


def code_map_query(args: Dict[str, Any]) -> str:
    """code_map_query tool handler. Paged symbol lookup."""
    name = args.get("name")
    path = args.get("path")
    kind = args.get("kind")
    root = args.get("root") or os.getcwd()
    limit = args.get("limit", PAGE_MAX_SYMBOLS)
    cursor = args.get("cursor", "0")
    root = os.path.abspath(root)

    db_path = root_db_path(root)
    if not db_path.exists():
        return json.dumps({
            "symbols": [], "count": 0, "limit": limit,
            "cursor": "0", "staleness": "partial",
            "note": "index not built for this root; "
                    "call code_map_confirm_root first",
        }, ensure_ascii=False)

    conn = init_db(root)
    try:
        t0 = time.perf_counter()  # L45
        staleness = _staleness(root, conn)  # L64
        offset = int(cursor) if cursor.isdigit() else 0
        rows = query_symbols(conn, root, name=name, path=path,
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


def code_map_confirm_root(args: Dict[str, Any]) -> str:
    """code_map_confirm_root: lock a root and trigger async build."""
    from .state import confirm_root
    file_path = args.get("file_path", "")
    root_path = args.get("root_path", "")
    result = confirm_root(root_path or file_path)
    return json.dumps(result, ensure_ascii=False)


def code_map_refresh(args: Dict[str, Any]) -> str:
    """code_map_refresh: authoritative re-scan (async, non-blocking)."""
    from .state import refresh_root
    root_path = args.get("root_path", os.getcwd())
    result = refresh_root(root_path)
    return json.dumps(result, ensure_ascii=False)
