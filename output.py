"""output.py — serialization helpers for code_map payloads.

Kept separate so the byte-budget truncation logic (gate ④/⑤) is testable
without importing the full query path.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List


def serialize_payload(symbols: List[dict], staleness: str,
                      limit: int, cursor: str,
                      disclosure: str = "") -> Dict[str, Any]:
    """Build the standard code_map_query response dict."""
    payload: Dict[str, Any] = {
        "symbols": symbols,
        "count": len(symbols),
        "limit": limit,
        "cursor": cursor,
        "staleness": staleness,
    }
    if disclosure:
        payload["disclosure"] = disclosure
    return payload


def truncate_to_budget(payload: Dict[str, Any],
                       budget_bytes: int) -> Dict[str, Any]:
    """Shrink `payload['symbols']` until the serialized JSON fits the budget.

    Drops symbols from the tail (least-recent / highest-line hits) one at a
    time. Returns the (possibly truncated) payload dict.
    """
    result = json.dumps(payload, ensure_ascii=False)
    while len(result.encode("utf-8")) > budget_bytes and payload.get("symbols"):
        payload["symbols"] = payload["symbols"][:-1]
        payload["count"] = len(payload["symbols"])
        result = json.dumps(payload, ensure_ascii=False)
    return payload
