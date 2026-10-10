"""code_map plugin — L1.5 code-symbol index (tree-sitter, zero external API).

Registers, cleanly and without touching built-ins:
  - 3 tools:  code_map_query / code_map_confirm_root / code_map_refresh
  - 1 hook:    transform_tool_result (Tier-1 injection after write_file/patch)

Deliberately NOT what code_intel does: no injection into built-in tool
descriptions, no monkey-patching _build_child_agent, no forcing a toolset into
every subagent. The only reach into other sessions is the single Tier-1
transform_tool_result hook, and it is fail-soft (returns None on any error).
"""
from __future__ import annotations

import os
import sys
from typing import Any

from ._dep_paths import ensure_dep_paths


def _ensure_code_map_dep_paths() -> None:
    """Make tree_sitter + the language grammars importable, UNIFORMLY from
    /root/.hermes/plugin-venv.

    The site-packages dir is resolved by globbing ``lib/python*`` under the
    venv (never a hardcoded minor version), so a venv built under 3.13, 3.14
    or a future 3.15 all resolve. This splices the dir onto sys.path so
    parser.py's module-level tree-sitter imports succeed. Fail-soft: if the
    venv is absent the resolver returns [] and we fall back to the host, never
    crash.

    Per user directive (10-08) this always points at the plugin venv — the
    previous "if the host already sees tree_sitter, skip" early-return is gone,
    so dep resolution is unified on ~/.hermes/plugin-venv, not "whatever the
    host happens to have".

    MUST run before the relative imports below pull in parser.py, whose
    module-level tree-sitter imports are what fail on a host that lacks them.
    """
    ensure_dep_paths()


_ensure_code_map_dep_paths()

from . import hook  # noqa: E402  (must follow the dep splice)
from .query import code_map_query, code_map_confirm_root, code_map_refresh  # noqa: E402


def register(ctx: Any) -> None:
    """Register the code_map surface. `ctx` is a PluginContext."""
    _register_tools(ctx)
    _register_hooks(ctx)


# ---- tools ---------------------------------------------------------------

_TOOLSET = "code_map"

_TOOL_SCHEMAS = {
    "code_map_query": {
        "name": "code_map_query",
        "description": (
            "Cross-file symbol location over the confirmed project index. "
            "Query by symbol name (jump-to-definition; lists all same-name "
            "hits, name-level best-effort) or by file path (single-file "
            "outline). Paged; pass cursor to continue. Returns a staleness "
            "flag: 'partial' means the index may lag in-place edits — call "
            "code_map_refresh to force a fresh re-scan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to locate across the repo"},
                "path": {"type": "string", "description": "File path to outline (single-file symbols)"},
                "kind": {"type": "string", "description": "Optional filter: function|class|method|import"},
                "root": {"type": "string", "description": "Optional explicit project root"},
                "limit": {"type": "integer", "description": "Max rows per page (default 200)"},
                "cursor": {"type": "string", "description": "Opaque cursor from a prior truncated page"},
            },
        },
    },
    "code_map_confirm_root": {
        "name": "code_map_confirm_root",
        "description": (
            "Confirm a project root so code_map can index it. Pass root_path to "
            "lock a specific root; or pass only file_path to get 4 candidate "
            "roots (nearest marker-bearing ancestors, capped at $HOME) to "
            "confirm. Triggers an async, non-blocking build."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "A file whose project root to resolve"},
                "root_path": {"type": "string", "description": "Explicit root to lock (skips candidate listing)"},
            },
        },
    },
    "code_map_refresh": {
        "name": "code_map_refresh",
        "description": (
            "Authoritative re-scan of a project root. Re-parses every file whose "
            "fingerprint changed, catching in-place content edits that the fast "
            "query path deliberately does not detect. Async, non-blocking."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "root_path": {"type": "string", "description": "Project root to refresh"},
            },
        },
    },
}

_TOOL_HANDLERS = {
    "code_map_query": code_map_query,
    "code_map_confirm_root": code_map_confirm_root,
    "code_map_refresh": code_map_refresh,
}


def _register_tools(ctx: Any) -> None:
    for name, schema in _TOOL_SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset=_TOOLSET,
            schema=schema,
            handler=_TOOL_HANDLERS[name],
            description=schema.get("description", ""),
        )


# ---- hooks ---------------------------------------------------------------

def _register_hooks(ctx: Any) -> None:
    # Tier-1 injection, fail-soft. Fires on every handle_function_call result;
    # the handler no-ops for anything that isn't a write_file/patch under a
    # confirmed root.
    ctx.register_hook("transform_tool_result", hook.on_write_result)
