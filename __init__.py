"""code_map plugin — L1.5 code-symbol index (tree-sitter, zero external API).

Registers, cleanly and without touching built-ins:
  - 4 tools:  code_map_query / code_map_confirm_root / code_map_refresh /
              code_map_check_root
  - 2 hooks:  transform_tool_result (read-side auto-attach + dirty self-heal)
              + post_tool_call (write-side success-gated single-file reindex)

Deliberately NOT what code_intel does: no injection into built-in tool
descriptions, no monkey-patching _build_child_agent, no forcing a toolset into
every subagent. Both hooks are fail-soft (return None on any error; a map
problem never breaks the underlying tool result).
"""
from __future__ import annotations

import os
import sys
from typing import Any

from ._dep_paths import ensure_dep_paths


def _ensure_code_map_dep_paths() -> None:
    """Make tree_sitter + the language grammars importable, UNIFORMLY from
    $HERMES_HOME/plugin-venv.
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


def _ensure_fcntl_module() -> None:
    """Make the bare ``import fcntl`` in hook.py/lockfile.py resolvable on Windows.

    The loader (hermes_cli.plugins_loader._load_directory_module) imports this
    package via file location and never puts the plugins dir on sys.path, so a
    top-level ``import fcntl`` fails on Windows, where the stdlib has no fcntl.
    Install the bundled fcntl.py shim (identical to the $HERMES_HOME/plugins
    shim) into sys.modules; no-op when a real fcntl (Linux/macOS) or an
    already-imported shim is present.
    """
    try:
        import fcntl  # noqa: F401  (stdlib on Linux/macOS, or shim already on path)
        return
    except ModuleNotFoundError:
        pass
    import importlib.util as _ilu
    shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fcntl.py")
    spec = _ilu.spec_from_file_location("fcntl", shim)
    if spec is None or spec.loader is None:
        raise ImportError(f"code_map: cannot load fcntl shim at {shim}")
    mod = _ilu.module_from_spec(spec)
    sys.modules["fcntl"] = mod
    spec.loader.exec_module(mod)


_ensure_fcntl_module()

from . import hook  # noqa: E402  (must follow the dep splice)
from .query import code_map_query, code_map_confirm_root, code_map_refresh, code_map_check_root  # noqa: E402

# 2B: system prompt section (borrowed from filemem dry-run/audit protocol).
# Frozen into each new session prompt via ctx.register_system_prompt_section
# (host-side interface at /usr/local/lib/hermes-agent/hermes_cli/plugins.py
# L957-982, verified this session). Content restates the dry-run-first policy,
# not a new convention, so it does not conflict with existing filemem/security
# sections.
_ROOT_POLICY_TEXT = (
    "code_map: to index a project you must pass a (root, path) pair — "
    "root is a boundary prefix (/root, /usr, /home, ...; never bare '/'), "
    "path is the RELATIVE sub-path inside it; both are mandatory, and the "
    "joined target is what gets indexed. A root-only call would index a "
    "whole boundary dir; a path-only call has no boundary — both are "
    "rejected. Before building, call code_map_check_root(root, path) "
    "(dry-run default) to preview how many files would be indexed, then "
    "ask the user via clarify — put the full target path IN the clarify "
    "question (e.g. '要 index /root/.hermes/plugins/filemem 吗？'); a "
    "TRAILING sub-path of >= 2 consecutive segments (e.g. 'plugins/filemem') "
    "also counts as mentioning the target (a bare single segment like just "
    "'filemem' does NOT count) — the gate's transcript lookup accepts "
    "either, so a partially-phrased question still records evidence. The "
    "host's transcript records which target the user is approving; the "
    "user's affirmative reply is the evidence a later yes=true build "
    "requires (blocked_by_policy without it). Build (yes=true) only "
    "after the user explicitly confirms. Confirmed targets are the ONLY "
    "code_map_query accepts: the call needs root + name (cross-file "
    "locate) or root + path (single-file outline, path relative to the "
    "confirmed root); unconfirmed or out-of-root paths are rejected. "
    "When a confirmed index already exists for the target you are "
    "working in: prefer code_map_query to locate symbols instead of "
    "grepping the whole tree; after editing files under that target, "
    "call code_map_refresh(root, path) so the index catches up before "
    "relying on it again. DELETIONS: the write-side hook observes "
    "write_file/patch only — the host's delete_file tool and terminal "
    "rm are NOT routed through the plugin's post_tool_call hook, so a "
    "deletion is invisible to it (this is why stale rows persist after "
    "you delete a file; there is deliberately no auto-refresh on the "
    "write path — a full rebuild there would bypass the read-side "
    "heal budgets). Stale symbol rows from a deleted file are cleared "
    "by the next full rescan (code_map_refresh) and by read-side heal "
    "for small dirty targets; if you delete files and then rely on "
    "symbol lookup, call code_map_refresh explicitly. "
    "Every build appends one audit line to "
    "data/confirm_root_log.jsonl (target + boundary + rel path + file "
    "count + db path), kept for recovery. "
    "TOOL CHOICE: when reading or writing SOURCE CODE, prefer the host's "
    "native code tools — read_file (a confirmed target's source read "
    "auto-attaches that file's symbol outline + import edges + "
    "cross-folder signal, so the map comes along for free) and "
    "write_file/patch (a successful write auto-reindexes the single "
    "file, keeping the map current). Use the terminal ONLY for what the "
    "native tools cannot do (builds, tests, git, processes, network); "
    "do not terminal cat/grep/sed/echo source files when read_file/"
    "search_files covers the need — that is exactly the gap this "
    "preference closes. (Owner's B-option, 2026-10-15: prompt-layer "
    "steering, no tool override.) "
    "SEARCH-SIDE DEFAULT (owner 2026-10-18, option 2): within a confirmed "
    "target, locate a symbol or a file BY NAME via code_map_query(root="
    "<confirmed target>, name=... or path=...) FIRST — one indexed "
    "round-trip instead of a whole-tree glob/grep. Honor the one-line "
    "[code_map] pointer a directory-anchored search_files (path=<dir> under "
    "a confirmed target) already carries. Fall back to whole-tree "
    "search_files/grep ONLY when the target is not confirmed, or the match "
    "is content/regex (which the index does not rank). Hard default."
)


def register(ctx: Any) -> None:
    """Register the code_map surface. `ctx` is a PluginContext."""
    _check_data_dir_sanity()
    _register_tools(ctx)
    _register_hooks(ctx)
    _register_prompt_section(ctx)


def _check_data_dir_sanity() -> None:
    """F7a (reviewer, minor): the whole cross-process lock scheme rests on
    every host/profile process resolving ``data_dir()`` to the SAME
    physical directory (verified this session via hardlink). If that
    invariant is silently broken — e.g. someone removed the hardlink and
    replaced it with a copy, or a deploy diverges — the flocks would be
    per-process no-ops (each locking its own private dir) and the build
    could corrupt the dbs via cross-file races WAL no longer catches
    (two distinct files). Detect it at register time and log a
    prominent, actionable warning. Fail-soft: a detection failure must
    never block plugin registration.

    We compare the data dir's device+inode against a reference written at
    the last successful register (data/_data_dir_fingerprint). A fresh
    install writes the fingerprint; a deploy that replaces the dir with a
    new physical one (different inode) gets a loud warning."""
    import logging
    import os
    try:
        from .config import data_dir
        dd = data_dir()
        dd.mkdir(parents=True, exist_ok=True)
        ref_path = dd / "_data_dir_fingerprint"
        st = os.stat(dd)
        ref = (st.st_dev, st.st_ino)
        if ref_path.exists():
            try:
                old_dev, old_ino = map(int, ref_path.read_text(encoding="utf-8", errors="replace").split())
                if (old_dev, old_ino) != ref:
                    logging.getLogger("code_map").warning(
                        "code_map: data dir physical identity changed since "
                        "last register (dev/ino %s -> %s). The cross-process "
                        "write lock (fcntl.flock on data/_proc.lock) is only "
                        "effective if ALL host/profile processes resolve "
                        "data_dir() to this SAME physical directory. If the "
                        "hardlink between profile paths and the shared data "
                        "dir was broken, replace the copy with a hardlink "
                        "(or re-point the profile at the original dir) or "
                        "concurrent multi-process builds may corrupt the db. "
                        "See reviewer F7a / the 2026-10-11 deploy notes.",
                        (old_dev, old_ino), ref)
            except Exception:
                pass
        ref_path.write_text(f"{st.st_dev} {st.st_ino}\n")
    except Exception:
        # detection is best-effort; never block registration on it
        pass


# ---- tools ---------------------------------------------------------------

_TOOLSET = "code_map"

_TOOL_SCHEMAS = {
    "code_map_query": {
        "name": "code_map_query",
        "description": (
            "Cross-file symbol location over the confirmed project index. "
            "ROOT IS REQUIRED and must be a confirmed root (unconfirmed or "
            "system-dir roots are rejected). Root and path are separate "
            "concerns: call with root+name for a cross-file locate, or "
            "root+path (path relative to root, or an absolute path inside "
            "root) for a single-file outline; a root-only or path-only call "
            "is invalid. Paged; pass cursor to continue. Returns a "
            "staleness flag: 'partial' means the index may lag in-place "
            "edits — call code_map_refresh to force a fresh re-scan. "
            "Responses are byte-capped: 'count' is rows on this page, "
            "'total' is ALL matching rows — always follow 'cursor' until "
            "a page returns count=0 before treating a set as complete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "root": {"type": "string",
                         "description": "REQUIRED: a confirmed project root"},
                "name": {"type": "string", "description": "Symbol name to locate across the repo"},
                "path": {"type": "string", "description": "File to outline: relative to root, or an absolute path that lies inside root"},
                "kind": {"type": "string", "description": "Optional filter: function|class|method|import|call_site|parameter|kwarg_usage"},
                "limit": {"type": "integer", "description": "Max rows per page (default 200)"},
                "cursor": {"type": "string", "description": "Opaque cursor from a prior truncated page"},
            },
            "required": ["root"],
        },
    },
    "code_map_confirm_root": {
        "name": "code_map_confirm_root",
        "description": (
            "Confirm an index target so code_map can build it. Gate (owner "
            "rule 2026-10-11 v2): BOTH root and path are mandatory — root is "
            "a boundary prefix (/root, /usr, /home, ...; never bare '/'), "
            "path is the RELATIVE sub-path inside it, and the joined target "
            "is what gets indexed. A root-only call (whole-boundary index, "
            "the 382k-file incident) or a path-only call is invalid; a path "
            "that escapes the boundary via ../ is invalid. Triggers an "
            "async, non-blocking build."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "root": {"type": "string",
                         "description": "Boundary prefix, e.g. /root or /usr/local (never '/')"},
                "path": {"type": "string",
                         "description": "Relative sub-path inside root, e.g. .hermes/plugins/filemem"},
            },
            "required": ["root", "path"],
        },
    },
    "code_map_refresh": {
        "name": "code_map_refresh",
        "description": (
            "Authoritative re-scan of a CONFIRMED index target. Pass the "
            "same (root, path) pair used at confirm time, or root_path as "
            "the full stored target. Unconfirmed targets are rejected. "
            "Async, non-blocking."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Boundary prefix as at confirm time"},
                "path": {"type": "string", "description": "Relative sub-path as at confirm time"},
                "root_path": {"type": "string", "description": "Full stored target (alternative to root+path)"},
            },
        },
    },
    "code_map_check_root": {
        "name": "code_map_check_root",
        "description": (
            "Safe confirm gate before building a code_map index. Same "
            "(root, path) pair contract as code_map_confirm_root: both "
            "mandatory, path relative inside root, bare '/' refused. "
            "DEFAULT is dry-run: reports how many indexable files live at "
            "the joined target, writing nothing. Pass yes=true to build "
            "(appends one audit line to data/confirm_root_log.jsonl). Use "
            "this INSTEAD of code_map_confirm_root when unsure whether a "
            "target is worth indexing — the dry-run preview is the whole "
            "point."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Boundary prefix, e.g. /root"},
                "path": {"type": "string", "description": "Relative sub-path inside root"},
                "yes": {"type": "boolean", "description": "Build for real (default false = dry-run preview only)"},
            },
            "required": ["root", "path"],
        },
    },
}

_TOOL_HANDLERS = {
    "code_map_query": code_map_query,
    "code_map_confirm_root": code_map_confirm_root,
    "code_map_refresh": code_map_refresh,
    "code_map_check_root": code_map_check_root,
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


def _register_prompt_section(ctx: Any) -> None:
    """2B: freeze the root-confirm policy into every new session prompt."""
    ctx.register_system_prompt_section("code_map_root_policy", _ROOT_POLICY_TEXT)


# ---- hooks ---------------------------------------------------------------

def _register_hooks(ctx: Any) -> None:
    # Tier-1 hooks, both fail-soft (gate 9).
    # - transform_tool_result: read-side auto-attach (件 2 + 件 3). The host
    #   prepends the returned string to the tool result; write-side is a
    #   no-op here (moved to post_tool_call below).
    # - post_tool_call: write/patch side. On a SUCCESSFUL write under a
    #   confirmed target, synchronously reindex that single file + mark the
    #   target dirty (件 1). Observer hook — returns None, mutates the db.
    #   ALSO: read-side ledger write for un-indexed projects (turn-boundary
    #   clarify, owner 2026-10-13) + clarify-answer settle (要/不要/不再问).
    # - pre_llm_call: the turn-boundary face (owner 2026-10-13) — at the start
    #   of each LLM turn, injects a notice listing this session's still-pending
    #   un-indexed project dirs, instructing the model to call the host
    #   `clarify` tool with three fixed options (要 / 不要 / 不再问). Returns
    #   None when nothing is pending. Mirrors filemem's auto-retrieve return
    #   shape ({'context': '...'}).
    ctx.register_hook("transform_tool_result", hook.on_result)
    ctx.register_hook("post_tool_call", hook._post_tool_call)
    ctx.register_hook("pre_llm_call", hook.on_pre_llm_call)

