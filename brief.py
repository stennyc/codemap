"""brief.py — L2.8h: frozen per-target project briefs for the root-policy
system-prompt section, plus the host section callable.

Design (owner rulings 2026-11-17, Q1-Q4, all as recommended):
- Each brief is a DETERMINISTIC function of (db bytes, source bytes,
  frozen_at label) — byte-stable for a session (the host renders the
  section ONCE per session and freezes it: prefix-cache stable, token
  cost irrelevant), re-frozen at each new session start.
- Only SLOW variables enter a brief (entry face, module shape,
  dependency skeleton, public symbol NAMES). NO line anchors — those live
  in the fresh layer (attach blocks / code_map_query payloads, each
  stamped with parser generation + staleness). The layer contract line
  says exactly that, so the model knows which layer it is reading.
- Q1: <=5 confirmed targets -> briefs for ALL; >5 -> the 3 most recent
  by db mtime + a "+N more" marker.
- Q2: the module role line is the module's first docstring line
  (<=56 chars, one-shot cost at freeze time).
- Q4: fail-soft everywhere — a broken db / unreadable source SKIPS that
  target (or the whole briefs block); a session section is never broken.
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, Mapping, Optional

from . import config, rootreg

# Q1 caps
BRIEF_MAX_TARGETS = 5      # <= 5 confirmed targets: brief every one
BRIEF_RECENT_TARGETS = 3   # > 5: only the 3 most recent (by db mtime)
BRIEF_MAX_LINES = 8        # hard cap per target block (Q1 ruling)
BRIEF_ROLE_MAX_CHARS = 56  # Q2: first docstring line truncation
BRIEF_MODS_PER_LINE = 4
BRIEF_MODULES_MAX = 8      # then "+N more"
BRIEF_PUB_MAX = 8          # then "(+K)"
BRIEF_DEPS_MAX_EDGES = 2
BRIEF_SOURCE_READ_MAX = 1_500_000  # cap per top-level file (monolith safety)

_ENTRY_DEF_RE = re.compile(r"^\s*def register\(", re.M)
# literal arg or name= kwarg (both registration shapes observed in the wild)
_TOOL_NAME_RE = re.compile(
    r"ctx\.register_tool\(\s*(?:name\s*=\s*)?['\"]([\w-]+)['\"]")
_HOST_HOOK_RE = re.compile(r"ctx\.register_(?!tool)(\w+)")
_DOCSTRING_RE = re.compile(r'^\s*(?:"""|\'\'\')(?P<body>[^\n]*)')


def _top_level_py_files(target: str) -> list:
    try:
        names = sorted(
            n for n in os.listdir(target)
            if n.endswith(".py") and os.path.isfile(os.path.join(target, n))
        )
    except OSError:
        return []
    return [os.path.join(target, n) for n in names]


def _first_docstring_line(src: str) -> str:
    m = _DOCSTRING_RE.match(src)
    if not m:
        return ""
    # a one-line docstring ("""...""" ) leaks its CLOSING quotes into the
    # capture — strip them (and any trailing quote runs) before display
    return m.group("body").strip().rstrip("\"'").strip()


def _entry_line(target: str, files: list) -> Optional[str]:
    """Plugin-shape entry face: a top-level `def register(ctx)` + the
    register_tool / register_* surfaces it declares. None for generic
    projects (the whole line is omitted — Q1/Q2: only slow variables)."""
    entry_sig = None
    tools: set = set()
    hooks: set = set()
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                src = fh.read(BRIEF_SOURCE_READ_MAX)
        except OSError:
            continue
        if _ENTRY_DEF_RE.search(src):
            entry_sig = "register(ctx)"
        tools |= set(_TOOL_NAME_RE.findall(src))
        hooks |= set(_HOST_HOOK_RE.findall(src))
    if not entry_sig:
        return None
    # "0 tools" is a false negative for loop/variable registrations
    # (register_tool(name=_tname, ...) — the count is only known at runtime
    # via get_tool_schemas) — omit the tools clause when no literal names
    # are visible rather than claiming zero.
    tail = f" · {len(tools)} tools" if tools else ""
    return f"entry: {entry_sig} · {len(hooks)} host hooks{tail}"


def _modules_lines(target: str, files: list) -> list:
    """`modules: a=role · b=role` wrapped at BRIEF_MODS_PER_LINE, max
    BRIEF_MODULES_MAX + overflow marker (Q2: role = first docstring line)."""
    entries = []
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0]
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                role = _first_docstring_line(fh.read(BRIEF_SOURCE_READ_MAX))
        except OSError:
            role = ""
        role = role[:BRIEF_ROLE_MAX_CHARS] + (
            "…" if len(role) > BRIEF_ROLE_MAX_CHARS else "")
        entries.append(f"{stem}={role}" if role else stem)
    lines = []
    head = entries[:BRIEF_MODULES_MAX]
    for i in range(0, len(head), BRIEF_MODS_PER_LINE):
        lines.append(" · ".join(head[i:i + BRIEF_MODS_PER_LINE]))
    if len(entries) > BRIEF_MODULES_MAX:
        lines[-1] += f" (+{len(entries) - BRIEF_MODULES_MAX} more)"
    return [f"modules: {l}" for l in lines] if lines else []


def _deps_line(target: str, conn: sqlite3.Connection) -> Optional[str]:
    """`deps: parent -> {child, ...}` over TOP-LEVEL parent files only
    (sibling relative imports: `from .X import ...` / `import X`)."""
    row = conn.execute(
        "SELECT path, sig FROM symbols WHERE kind='import' "
        "AND (sig LIKE 'from .%' OR sig LIKE 'import %')").fetchall()
    stems = {os.path.splitext(os.path.basename(f))[0]
             for f in _top_level_py_files(target)}
    root_norm = target.replace(os.sep, "/")
    edges: dict = {}
    for path, sig in row:
        parent_dir = "/".join(os.path.dirname(path).split(os.sep))
        if parent_dir != root_norm:
            continue  # only top-level parents are skeleton-level
        sig = sig.strip()
        if sig.startswith("from ."):
            toks = sig.split()
            if len(toks) >= 4 and toks[1] == "import":
                child = toks[3].split(".")[0]   # `from . import X`
            else:
                child = toks[1].split(".")[0] if len(toks) > 1 else ""  # `from .X import ...`
        elif sig.startswith("import "):
            child = sig.split()[1].split(".")[0]
        else:
            continue
        if child in stems:
            parent = os.path.splitext(os.path.basename(path))[0]
            edges.setdefault(parent, set()).add(child)
    if not edges:
        return None
    # deterministic pick: top BRIEF_DEPS_MAX_EDGES parents by child count
    picked_parents = sorted(edges, key=lambda p: (-len(edges[p]), p))[:BRIEF_DEPS_MAX_EDGES]
    parts = [f"{p} -> {{{', '.join(sorted(edges[p]))}}}" for p in picked_parents]
    return "deps: " + " · ".join(parts)


def _public_line(conn: sqlite3.Connection, target: str) -> Optional[str]:
    """def-side public symbols ranked by USAGE rows (L2.4c heat metric:
    def-side names by cross-file consumer volume), EXCLUDING defs under the
    target's tests/ and tools/ DIRS (raw heat is contaminated by test
    fixtures — the brief must show production symbols, not `fake`/
    `_boom`). The exclusion is ANCHORED to <target>\\tests\\ /
    <target>\\tools\\ (not a bare '%tests%' substring — a pytest tmp
    dir named after a test function contains 'tests' in its own path and
    would nuke the whole CTE). Names only, no line anchors; zero-usage
    names are noise (no navigation signal) — omitted."""
    ex_tests = f"{target}{os.sep}tests{os.sep}%"
    ex_tools = f"{target}{os.sep}tools{os.sep}%"
    # underscore-prefixed names are private implementation helpers (the
    # L2.4c heat shows test/fixture helpers dominate raw usage counts) —
    # the brief's public line is for NAVIGATION targets, not privates.
    names = [r[0] for r in conn.execute(
        "WITH defs AS (SELECT DISTINCT name FROM symbols "
        "WHERE kind IN ('function','method','class') "
        "AND path NOT LIKE ? AND path NOT LIKE ? "
        "AND name NOT LIKE '\\_%' ESCAPE '\\'), "
        "usage AS (SELECT name, COUNT(*) n FROM symbols "
        "WHERE kind IN ('call_site','kwarg_usage','import') "
        "GROUP BY name) "
        "SELECT d.name FROM defs d LEFT JOIN usage u ON u.name = d.name "
        "WHERE COALESCE(u.n, 0) > 0 ORDER BY u.n DESC, d.name LIMIT ?",
        (ex_tests, ex_tools, BRIEF_PUB_MAX)).fetchall()]
    if not names:
        return None
    return f"public: {' · '.join(names)}"


def build_target_brief(target: str, frozen_at: str) -> Optional[str]:
    """Render the <= BRIEF_MAX_LINES block for one confirmed target.

    Returns None on ANY failure (fail-soft, Q4): missing db, corrupt db,
    unreadable source — a target that cannot be briefed is simply absent
    from the section, and the section never breaks.
    """
    try:
        db_path = config.root_db_path(target)
        if not db_path.exists():
            return None
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            from .query import _staleness
            status = _staleness(target, conn)  # disk walk; fresh/partial/...
            deps = _deps_line(target, conn)
            pub = _public_line(conn, target)
        finally:
            conn.close()
    except Exception:
        return None

    name = os.path.basename(target.rstrip(os.sep)) or target
    files = _top_level_py_files(target)
    lines = [
        f"## {name} ({target})",
        f"index: gen {config.PARSER_GENERATION} · {rows} rows · "
        f"{status}@freeze({frozen_at})",
    ]
    entry = _entry_line(target, files)
    if entry:
        lines.append(entry)
    lines.extend(_modules_lines(target, files))
    if deps:
        lines.append(deps)
    if pub:
        lines.append(pub)
    lines.append("layer: frozen layer — may lag; line anchors: attach/query "
                 "blocks (each stamped gen+staleness)")
    return "\n".join(lines[:BRIEF_MAX_LINES])


def render_briefs(frozen_at: str, cap: int = BRIEF_MAX_TARGETS,
                  recent: int = BRIEF_RECENT_TARGETS) -> str:
    """Briefs for the confirmed targets (Q1 cap + recency rule). '' when
    nothing renders."""
    confirmed = rootreg.confirmed_targets()
    with_db = [t for t in confirmed if config.root_db_path(t).exists()]
    extra = 0
    if len(with_db) > cap:
        chosen = sorted(
            with_db,
            key=lambda t: os.path.getmtime(str(config.root_db_path(t))),
            reverse=True)[:recent]
        extra = len(with_db) - recent
    else:
        chosen = with_db
    blocks = [b for b in (build_target_brief(t, frozen_at) for t in sorted(chosen))
              if b]
    text = "\n".join(blocks)
    if extra:
        tail = (f"…(+{extra} more confirmed targets — brief omitted; "
                f"code_map_query for detail)")
        text = (text + "\n" + tail) if text else tail
    return text


def section_content(session_info: Mapping[str, Any]) -> str:
    """Host section callable (L2.8h). The host renders it ONCE per session
    and freezes the result into the session prompt (byte-stable within the
    session -> prefix-cache friendly; re-frozen per new session). The
    frozen_at label is the session id — deterministic per session.
    Fail-soft (Q4): any brief error -> the static policy text only."""
    from . import _ROOT_POLICY_TEXT
    base = _ROOT_POLICY_TEXT
    try:
        frozen_at = str((session_info or {}).get("session_id") or "sess")
        rendered = render_briefs(frozen_at)
        return base + ("\n\n" + rendered if rendered else "")
    except Exception:
        return base
