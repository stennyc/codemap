"""Configuration + path/limit constants for the code_map plugin.

Kept dependency-free (stdlib only) so any module can import it and so tests
can override the data dir via env without touching ~/.hermes.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# ---- limits (all from the sealed contract, not re-derived) -----------------
# Single-page output budget. USER-SEALED: 1500 tokens ≈ 6KB. 200 symbols is a
# structural backstop for an abnormally dense page; the byte budget is the
# PRIMARY cap, measured AFTER serialization so big signatures/docstrings can't
# sneak past. (An earlier build reconciled this down to 1KB against the PRD
# gate-table; the user re-affirmed 1500 tokens, so the 6KB value is final.)
PAGE_BUDGET_BYTES = 6000        # ~1500 tokens
PAGE_MAX_SYMBOLS = 200          # structural backstop
# F-minor: query gate is measured on the 5k-file fixture and INCLUDES the B+
# scandir diff cost.
QUERY_GATE_MS = 50

# Walk / estimation gate (contract ⑤): pure stat-only walk of a candidate root
# must complete <2s; above 20k files we warn + invite free-input narrowing.
WALK_ESTIMATE_BUDGET_S = 2.0
WALK_WARN_FILE_COUNT = 20_000

# Per-root pruning blacklist (contract ⑤). .gitignore is HIGHEST priority and
# may override these; a business dir that is neither blacklisted nor ignored
# must NEVER be pruned (contract ③ "不漏业务目录").
DEFAULT_PRUNE_BLACKLIST = frozenset({
    "node_modules", ".venv", "venv", "env", ".env", ".git",
    "__pycache__", "dist", "build", ".tox", "target", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".nox", ".eggs", "*.egg-info",
    ".idea", ".vscode",
})

# File extensions code_map indexes (expanded from Python-only to 9 languages).
INDEXED_EXTENSIONS = frozenset({
    ".py",      # Python
    ".ts", ".tsx",  # TypeScript
    ".js", ".jsx",  # JavaScript
    ".go",      # Go
    ".rs",      # Rust
    ".java",    # Java
    ".dart",    # Dart
    ".c", ".h",  # C
    ".cpp", ".cc", ".cxx",  # C++
    ".hpp", ".hxx", ".hh",  # C++ headers
})

# Project-root markers, searched upward. `.git` has the highest priority
# (contract ③); language manifests are the fallback.
ROOT_MARKERS = (
    ".git",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Cargo.toml",
    "go.mod",
    "package.json",
    "pom.xml",
    "build.gradle",
    "CMakeLists.txt",
)

# Gate ⑩ (entry-legality, new): hard-refuse these as `confirm_root`/
# `refresh_root` inputs. A root at or under any of these is a whole-
# filesystem / home-dir level, not a project root — the 2026-10-10
# `/root` mis-confirm (382k files, 4.2GB db) is the recorded incident that
# motivated this constant.
BLOCKED_ROOT_PREFIXES = ("/", "/root", "/home", "/usr", "/etc", "/opt")

# SQLite tuning (contract ⑦): WAL + busy_timeout + per-file single-writer lock.
WAL_BUSY_TIMEOUT_MS = 5000
# Tier-1 save-hook debounce: agent writes N files in a burst -> one parse job.
HOOK_DEBOUNCE_S = 0.25

# Cross-folder dependency signal (owner 2026-10-13): appended AFTER the
# byte-capped symbol/import block of the read-side attach, as a one-line
# existence+count signal. `N` = how many of this file's in-project
# (root-internal) import edges land in a different top-level sub-directory
# of the CONFIRMED root than the file itself. Root-external targets
# (stdlib / third-party / anything above the confirmed root) are NEVER
# counted — the confirmed root is the project boundary. Budget: the signal
# line is appended OUTSIDE READ_SYMBOL_MAX_BYTES so it must never squeeze
# the symbol lines (owner: "预算不是重点，重点是不要影响分符号行"). Only
# emitted when N > 0; a 0 stays silent (no noise line). Sized so the FULL
# cue ("...more imports may exist beyond top-N -> code_map_query for the
# full list") survives for any realistic count (worst case count=3-digit
# number keeps the line at 204 bytes, under the 208 cap) — the follow-up
# keyword `code_map_query` must never be the truncated tail.
CROSS_FOLDER_LINE_MAX_BYTES = 208
# Read-side auto map-attach (code_map auto-indexing, owner 2026-10-11):
# when read_file/search_files lands on a source file UNDER a confirmed target,
# the result is auto-augmented with that file's own symbol outline (top
# READ_SELF_SYMBOLS) + its cross-file import edges (top READ_IMPORT_EDGES),
# so the model gets project-aware context without a separate code_map_query
# round-trip. Triggers ONLY for extensions in INDEXED_EXTENSIONS that are
# actually source (not .md/.json/.lock/binary). Total attached payload is
# capped at READ_SYMBOL_MAX_BYTES (~375 tokens) so the hot path never bloats.
READ_SELF_SYMBOLS = 30
READ_IMPORT_EDGES = 8
READ_SYMBOL_MAX_BYTES = 1500
# 件3 review fix (reviewer F1/F2, 2026-10-11): the hot-path dirty heal runs a
# FULL rebuild of the target (state._run_build — walk + parse). The host's
# transform_tool_result / post_tool_call are in _HOOK_TIMEOUT_BOUNDED_HOOKS
# (default 30s, fail-open skip + 60s suppression). A full rebuild on a LARGE
# target (reviewer measured 28.9s for 30k files cold) exceeds that budget and
# gets the hook skipped, leaving the worker abandoned mid-build AND the dirty
# stamp uncleared -> every subsequent read re-triggers a full rebuild. The
# two guards below bound this:
# - READ_HEAL_MAX_FILES: only attempt the sync heal when the target's current
#   index has <= this many files (the ms-scale regime the design comment
#   assumes). Above the threshold, skip the heal, attach from the STALE db
#   as-is, and CLEAR the dirty stamp so reads stop hammering full rebuilds.
READ_HEAL_MAX_FILES = 2000
# - READ_HEAL_MAX_ATTEMPTS: give up on the stamp after N consecutive failed
#   heals (corrupt db / lock). Without this, a persistently failing rebuild
#   turns every source-file read into a full-walk attempt. After the cap the
#   target stays dirty (code_map_refresh can still clear it on demand); reads
#   just stop trying.
READ_HEAL_MAX_ATTEMPTS = 3

# Build-path commit batching (gate ① wall-time fix). The build loop upserts
# files with the NO-COMMIT variant and issues one WAL commit per this many
# files, turning 500 per-file fsyncs into ~500/N. The hook/refresh path keeps
# per-file commit semantics untouched (gate ⑦ preserved: the in-process write
# lock is held per file only, released before commit, so a concurrent hook
# reindex on another file interleave at the file granularity). 32 keeps the
# worst-case commit wait for a concurrent writer bounded (~32 file DML ops).
BUILD_COMMIT_BATCH = 32


def data_dir() -> Path:
    """Where per-root SQLite DBs live. Honors HERMES_HOME so tests isolate.

    NOTE (F7a / concurrency harden): every host/profile process MUST resolve
    this to the SAME physical directory — the cross-process write lock
    (fcntl.flock on data/_proc.lock) only works if the lock file itself is
    a single physical file shared by all writers. The deploy hardlinks the
    profile-specific `plugins/code_map/data` path onto a shared physical dir;
    register() (in __init__.py) writes data/_data_dir_fingerprint (dev/ino)
    on every startup and warns LOUDLY if the physical identity has changed
    since the last register. A broken hardlink (e.g. a copy instead of a
    link) silently defeats the whole lock scheme — that warning is the
    deploy's only alarm."""
    home = os.environ.get("HERMES_HOME")
    if home:
        base = Path(home)
    else:
        try:
            # Hermes resolves its own home (per-profile). Fall back to ~/.hermes.
            from hermes_constants import get_hermes_home  # type: ignore
            base = Path(get_hermes_home())
        except Exception:
            base = Path.home() / ".hermes"
    d = base / "plugins" / "code_map" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def root_db_path(root: str) -> Path:
    """Deterministic per-root DB path under the shared data dir."""
    import hashlib
    digest = hashlib.sha1(os.path.abspath(root).encode("utf-8")).hexdigest()[:16]
    return data_dir() / f"{digest}.db"
