"""Configuration + path/limit constants for the code_map plugin.

Kept dependency-free (stdlib only) so any module can import it and so tests
can override the data dir via env without touching ~/.hermes.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# ---- limits (all from the sealed contract, not re-derived) -----------------
# Single-page output budget. USER-SEALED: 1500 tokens ≈ 6KB. 200 symbols is a
# structural backstop for an abnormally dense page; the byte budget is the
# PRIMARY cap, measured AFTER serialization so big signatures/docstrings can't
# sneak past. (An earlier build reconciled this down to 1KB against the PRD
# gate-table; the user re-affirmed 1500 tokens, so the 6KB value is final.)
PAGE_BUDGET_BYTES = 6000        # ~1500 tokens
PAGE_MAX_SYMBOLS = 200          # structural backstop
# F-minor: query gate is measured on the 5k-file fixture. W1 (780c58a,
# 2026-11-17): the exact-branch query_ms t0 now starts AFTER the staleness
# fingerprint walk (on_disk_files + diff_indexed), so the gated window is
# the query layer only; the walk still runs (staleness reporting unchanged)
# but no longer inflates the metric (busy-host 133-file walk dominated it).
QUERY_GATE_MS = 50

# L2.3c (2026-11-17): parser-generation stamp. Cross-generation phantom
# rows (the L2.7 campaign: 55 phantom kwarg rows in an 18k-line file
# written by an EARLIER parser/grammar generation, invisible to the
# source-mtime fingerprint, reported "fresh" by staleness) are killed by
# stamping every files-table row with the generation that parsed it:
# scanner re-parses on mismatch, _staleness surfaces "parser_gen_stale",
# a no-grammar hook reindex leaves the files row untouched. BUMP THIS
# ON EVERY PARSER-OR-GRAMMAR CHANGE (parser.py, grammar installs) so the
# next build re-parses; the legacy '' stamp (pre-L2.3c dbs) is always
# stale by design — the first post-upgrade build self-heals them.
# l2.8g-1 (2026-11-17): grammar-install generation — dep_install auto-
# heal landed; the next build re-parses under the wider grammar set.
PARSER_GENERATION = "l2.8g-1"

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

# File extensions code_map indexes (expanded from Python-only to 11 languages).
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
    ".php",     # PHP
    ".kt", ".kts",  # Kotlin
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

# OS auto-detection (owner 2026-11-16): the POSIX-only tuple above is dead on
# Windows — it never matches a Windows path, so the gate-⑩ shape had no
# Windows twin. The Windows system-tree list mirrors the POSIX semantics:
# system dirs + the bare filesystem root. C:\Users is deliberately NOT
# guarded (the owner workspace — the exact twin of POSIX /root//home,
# which the hook docstring keeps unguarded so real owner projects stay
# indexable).
BLOCKED_ROOT_PREFIXES_WIN = (
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "C:\\ProgramData",
)
IS_WINDOWS = sys.platform == "win32"
_BARE_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]?$")


def is_bare_fs_root(root: str, is_windows: bool) -> bool:
    """True when `root` names the whole filesystem level.

    POSIX: "" or "/". Windows: a bare drive root — "C:\\", "C:/" or the
    drive-relative marker "C:" (all normalize to 'index everything on that
    drive'). Pure string check (no os.path) so it stays testable on any
    host regardless of the host OS.
    """
    r = (root or "").strip()
    if is_windows:
        return _BARE_DRIVE_RE.match(r) is not None
    return r in ("", "/")


def is_system_tree(root: str, is_windows: bool) -> bool:
    """True when `root` is a bare fs root or sits under a per-OS system tree
    (the gate-⑩ incident shape — never indexable, never offered).

    Semantics are the LIVE guard (single source of truth for rootreg's
    join gate and the hook ledger's skip guard). POSIX keeps the legacy
    triple ("/usr", "/etc", "/opt" — /root//home deliberately unguarded:
    owner projects live there, owner 2026-10-18) plus the bare "/";
    Windows adds its system dirs + bare drive roots. Case- and
    separator-insensitive on Windows (mixed / and \\ accepted).
    """
    r = (root or "").strip()
    if is_windows:
        if _BARE_DRIVE_RE.match(r):
            return True
        lr = r.lower().replace("/", "\\")
        for p in BLOCKED_ROOT_PREFIXES_WIN:
            pl = p.lower()
            if lr == pl or lr.startswith(pl + "\\"):
                return True
        return False
    if r in ("", "/"):
        return True
    for p in ("/usr", "/etc", "/opt"):
        if r == p or r.startswith(p + "/"):
            return True
    return False


# Never-log transient markers (owner 2026-11-17 "临时目录永不再问" ruling):
# directory names that, anywhere on a resolved boundary path, mark a
# transient / non-source location code_map must NEVER propose for indexing —
# caches, scratch space, handoff data, pyc caches, plus the specific
# hermes-file-memory-plugin skill dir the owner ruled "never propose".
# Distinct from DEFAULT_PRUNE_BLACKLIST (the scanner's "skip this subtree
# INSIDE a confirmed root"): that prunes a walk; this vetoes the clarify-ledger
# path outright for a boundary that is a cache/data/skill tree, not a source
# project. Marker match is case-insensitive on Windows (dirs are
# case-insensitive there), mirroring is_system_tree.
TRANSIENT_DIR_NAMES = frozenset({
    "cache", ".cache", "scratch", "spillover", "handoffs", "__pycache__",
    "hermes-file-memory-plugin",
    "hermes-code-map",  # skill-dir "不再问" (owner 2026-11-17); distinct from the
                       # code_map PLUGIN (leaf 'code_map', underscore) — no false veto
    "autonomous-ai-agents",  # skills CLASS dir "永不再问" (owner 2026-11-17 row-2
                       # ruling): markdown skill docs' parent, never a source project;
                       # component-match vetoes the whole class (all its skill
                       # subdirs) — no legitimate source project uses this leaf name
})


def is_transient_dir(path: str, is_windows: bool = False) -> bool:
    """True when `path` is (or sits under) a transient / non-source directory
    that must never be proposed for indexing — the owner's 2026-11-17
    "临时目录永不再问" ruling (the 12-row clogged-ledger shape: hermes's own
    cache/scratch/spillover/handoffs trees).

    Pure string check (no fs access), separator-agnostic (normalizes both
    "/" and "\\"). Tests every path component against TRANSIENT_DIR_NAMES; a
    match means the resolved boundary is inside a cache/data tree, which
    detect_root could not anchor to a source project (no marker above it).
    `is_windows` makes the marker match case-insensitive, mirroring
    is_system_tree; POSIX keeps case. Testable on any host."""
    comps = [c for c in (path or "").replace("\\", "/").split("/") if c]
    if is_windows:
        comps = [c.lower() for c in comps]
    return any(c in TRANSIENT_DIR_NAMES for c in comps)


def is_container_root(proj: str, is_windows: bool = False) -> bool:
    """True when `proj` is EXACTLY one of the hermes-internal BARE container
    roots — HERMES_HOME itself, HERMES_HOME/profiles, and (F5, owner 2026-11-17)
    the OS local-app-data BARE root (Windows, from LOCALAPPDATA) and the
    home-dir .hermes relay/phantom mirror (both OSes, from Path.home()) —
    the 'never propose indexing the whole container' case ('子目录安全的容器根
    屏蔽'). Subdirectory-safe by construction: only the bare root matches
    (exact, separator/case-normalized comparison on Windows); a sub-project
    UNDER any of them (e.g. .../hermes/plugins/filemem) is NOT a container
    root and stays indexable. All roots are DERIVED from live env/home
    (hermes_home() + LOCALAPPDATA + Path.home()), never hardcoded."""
    target = (proj or "").strip()
    if not target:
        return False
    base = hermes_home()
    candidates = [base, base / "profiles"]
    # F5 (owner 2026-11-17): the two residual 12-row-clog container roots —
    # the OS local-app-data BARE root (Windows, from the live LOCALAPPDATA
    # env, not hardcoded) and the home-dir .hermes relay/phantom mirror
    # (both OSes, from Path.home(), not hardcoded). Subdir-safe: only the
    # bare root matches, so a sub-project under any of them stays open.
    if is_windows:
        la = os.environ.get("LOCALAPPDATA")
        if la:
            candidates.append(Path(la))
            # F6/row-1 (owner 2026-11-17): the SHARED hermes home
            # (LOCALAPPDATA/hermes) — distinct from the profile home that
            # hermes_home() returns on this host. Subdir-safe: only the bare
            # shared home matches, so .../hermes/plugins/<name> stays open.
            candidates.append(Path(la) / "hermes")
            # F-A (owner 2026-11-17): the SHARED profiles root
            # (LOCALAPPDATA/hermes/profiles, parent of every profile home).
            # hermes_home() returns the PROFILE home (.../profiles/coder), so
            # base/profiles was a phantom and this real shared root was NOT a
            # candidate (the reviewer's 复盘 gap). Subdir-safe: only the bare
            # .../hermes/profiles matches, so a profile home under it stays open.
            candidates.append(Path(la) / "hermes" / "profiles")
    # F3 (owner 2026-11-17): guard home resolution — Path.home() can raise
    # (RuntimeError/OSError) when no home is defined; degrade to skipping the
    # .hermes mirror candidate rather than crashing the whole predicate (which
    # fail-soft would swallow, silently disabling the container guard).
    try:
        candidates.append(Path.home() / ".hermes")
    except (RuntimeError, OSError):
        pass
    candidates = tuple(str(c) for c in candidates)
    # F4 (owner 2026-11-17): strip trailing separators from a hand-crafted
    # target (e.g. "C:\...\hermes\") so it still matches a candidate.
    want_target = target.rstrip("\\/")
    if not want_target:
        want_target = target
    if is_windows:
        want = want_target.replace("/", "\\").lower()
        for cand in candidates:
            if want == cand.replace("/", "\\").lower():
                return True
        return False
    for cand in candidates:
        if want_target == cand:
            return True
    return False


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

# L2.8h (owner Q3 ruling 2026-11-17): the attach-block staleness stamp runs
# the fingerprint disk walk ONLY when the target's indexed file count is
# within this cap (checked via the files table, O(1) — the walk itself is
# O(files) and would be too hot on big targets at read frequency). Above
# the cap the stamp degrades to "unknown(large target)" instead of lying.
ATTACH_STALENESS_WALK_MAX_FILES = 50_000
# 档 2 terminal read-signal (owner 2026-10-15): a one-line pointer appended
# to terminal stdout when a single-file cat/less of a confirmed-target
# source file is detected. Deliberately a POINT (byte-capped), NOT a full
# symbol attach — terminal stdout is opaque and must never be polluted with
# 30 symbols. Sized so "code_map_query" survives truncation (same rationale
# as CROSS_FOLDER_LINE_MAX_BYTES: the follow-up keyword must not be the
# truncated tail). 256 keeps the full cue under the cap for realistic paths.
TERMINAL_READ_NUDGE_MAX_BYTES = 256
# Search-side directory pointer (owner 2026-10-18): a one-line [code_map]
# POINT appended to a search_files result when its `path` (the search root)
# is a DIRECTORY under a confirmed target. A directory search root has no
# per-file symbol table to attach (a glob returns many paths), so — same
# discipline as the terminal cat nudge — this is a POINTER, not a 30-symbol
# block. It names the confirmed root and carries the follow-up keyword
# `code_map_query`, sized so that keyword survives truncation. 360 (raised
# from 320, P3-2) keeps the full cue — including the "whole-tree glob/grep"
# frame at the tail — under the cap for the longest realistic (root,
# target) pair (reviewer measured 347 bytes at 320, which clipped the tail).
SEARCH_DIR_NUDGE_MAX_BYTES = 360
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

# L2.4 concept bridge (owner 2026-11-16): rarity ranking (the GROUP BY JOIN
# pass) only runs when the candidate set is at or under this size — above
# it the query takes the no-rarity fast path (occ=0) so the budget holds
# on 30k-row roots.
CONCEPT_RARITY_MAX_CANDIDATES = 2000
# The concept bridge is scan-bound (word-surface LIKE over the root's rows):
# its own budget, distinct from the sealed QUERY_GATE_MS=50 which covers the
# EXACT name/path query paths (index-driven, must stay <50ms). Measured
# 2026-11-16: 20k-row root, substring token ~83ms worst case -> 150ms cap.
CONCEPT_QUERY_GATE_MS = 150

# L2.5 multi-hop impact propagation (owner 2026-11-16, roadmap ①):
# depth=N walks the name<->file bipartite frontier in N stages (odd stages
# = consumer rows of the frontier names, even stages = def-side symbols in
# those files). The cap keeps a single call bounded on dense roots; the
# frontier IN-lists are capped separately (SQLite bind-variable limit).
HOP_MAX_DEPTH = 3            # max propagation stages per call (cycle-safe)
HOP_FRONTIER_CAP = 500      # per-stage IN-list cap

# Build-path commit batching (gate ① wall-time fix). The build loop upserts
# files with the NO-COMMIT variant and issues one WAL commit per this many
# files, turning 500 per-file fsyncs into ~500/N. The hook/refresh path keeps
# per-file commit semantics untouched (gate ⑦ preserved: the in-process write
# lock is held per file only, released before commit, so a concurrent hook
# reindex on another file interleave at the file granularity). 32 keeps the
# worst-case commit wait for a concurrent writer bounded (~32 file DML ops).
BUILD_COMMIT_BATCH = 32


def hermes_home() -> Path:
    """The live hermes home base (HERMES_HOME) — the directory that data_dir()
    is rooted under (<home>/plugins/code_map/data). SINGLE SOURCE OF TRUTH for
    'where is the hermes home': data_dir() and is_container_root both derive
    from it, so the base can't drift. Resolves the same way as the old inline
    data_dir() resolution but returns the base WITHOUT the plugins/code_map/
    data suffix and WITHOUT the mkdir side-effect (owner 2026-11-17 F1: the
    predicate path is now pure; F2: no 3-level layout coupling). Precedence:
    HERMES_HOME env > hermes_constants.get_hermes_home() > per-OS canonical
    data dir."""
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home)
    try:
        # Hermes resolves its own home (per-profile).
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        if IS_WINDOWS:
            # OS auto-detect (owner 2026-11-16): the canonical Windows data dir
            # is %LOCALAPPDATA%\hermes. Windows must NEVER fall back to a
            # home-dir .hermes (the POSIX ~/.hermes fallback created a phantom
            # second tree on this host — C:\Users\user\.hermes forensics).
            local_appdata = os.environ.get("LOCALAPPDATA") or os.path.join(
                os.path.expanduser("~"), "AppData", "Local"
            )
            return Path(local_appdata) / "hermes"
        return Path.home() / ".hermes"


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
    d = hermes_home() / "plugins" / "code_map" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def root_db_path(root: str) -> Path:
    """Deterministic per-root DB path under the shared data dir."""
    import hashlib
    digest = hashlib.sha1(os.path.abspath(root).encode("utf-8")).hexdigest()[:16]
    return data_dir() / f"{digest}.db"
