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

# SQLite tuning (contract ⑦): WAL + busy_timeout + per-file single-writer lock.
WAL_BUSY_TIMEOUT_MS = 5000
# Tier-1 save-hook debounce: agent writes N files in a burst -> one parse job.
HOOK_DEBOUNCE_S = 0.25

# Build-path commit batching (gate ① wall-time fix). The build loop upserts
# files with the NO-COMMIT variant and issues one WAL commit per this many
# files, turning 500 per-file fsyncs into ~500/N. The hook/refresh path keeps
# per-file commit semantics untouched (gate ⑦ preserved: the in-process write
# lock is held per file only, released before commit, so a concurrent hook
# reindex on another file interleave at the file granularity). 32 keeps the
# worst-case commit wait for a concurrent writer bounded (~32 file DML ops).
BUILD_COMMIT_BATCH = 32


def data_dir() -> Path:
    """Where per-root SQLite DBs live. Honors HERMES_HOME so tests isolate."""
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
