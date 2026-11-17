"""L2.8h: frozen per-target project brief + section callable (TDD).

The brief is a deterministic function of (db bytes, source bytes, frozen_at
label) — byte-stable within a session (prefix-cache frozen layer), re-frozen
per new session (owner rule 2026-11-17: injection stability > token cost).
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_imports() -> None:
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)


def _seed_brief_fixture(tmp_path, monkeypatch) -> str:
    """HERMES_HOME-isolated PLUGIN-LIKE project 'briefproj':
    __init__.py: docstring + register(ctx) + ctx.register_tool x2
    search_index.py: docstring + def build_search_axes
    shared_memory.py: docstring + def sync_on_write
    main.py: def main + import/call rows for build_search_axes + sync_on_write
    tests/test_x.py: def fake_only_test (heat exclusion)
    longdoc.py: 130-char docstring line (role truncation)
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _fresh_imports()
    from code_map import db as cdb
    from code_map import rootreg, config

    proj = tmp_path / "briefproj"
    (proj / "tests").mkdir(parents=True)
    # NB: init_db backfill-parses on-disk source on first open (tree-sitter
    # is available in the test venv), so the REAL usage rows (import/call
    # sites) come from the source below — the hand-insert rows carry only
    # the def-side symbols + the plugin entry markers.
    files = {
        "__init__.py": (
            '"""Plugin entry: provider monolith."""\n'
            "def register(ctx):\n"
            '    ctx.register_tool("alpha")\n'
            '    ctx.register_tool("beta")\n'
        ),
        "search_index.py": (
            '"""Multi-axis RRF search core.\n"""\n'
            "def build_search_axes():\n    pass\n"
        ),
        "shared_memory.py": (
            '"""Central PG shared face\n"""\n'
            "def sync_on_write():\n    pass\n"
        ),
        "main.py": (
            "from .search_index import build_search_axes\n"
            "from .shared_memory import sync_on_write\n"
            "def main():\n    build_search_axes()\n    sync_on_write()\n"
        ),
        "tests/test_x.py": "def fake_only_test():\n    pass\n",
        "longdoc.py": '"""' + "L" * 130 + '.\n"""\n',
    }
    for rel, src in files.items():
        (proj / rel).write_text(src, encoding="utf-8")
    root = str(proj)
    rootreg.write_confirmed_root(root)

    p = Path(root)
    rows = {
        "__init__.py": [
            ("register", "function", 2, "def register(ctx):"),
        ],
        "search_index.py": [
            ("build_search_axes", "function", 2, "def build_search_axes():"),
        ],
        "shared_memory.py": [
            ("sync_on_write", "function", 2, "def sync_on_write():"),
        ],
        "main.py": [
            ("main", "function", 3, "def main():"),
            # init_db does NOT parse source (no backfill) — the real usage /
            # import rows come from these hand inserts (source + db agree):
            ("build_search_axes", "import", 1,
             "from .search_index import build_search_axes"),
            ("build_search_axes", "call_site", 4, "build_search_axes()"),
            ("sync_on_write", "import", 2,
             "from .shared_memory import sync_on_write"),
            ("sync_on_write", "call_site", 5, "sync_on_write()"),
        ],
        "tests/test_x.py": [
            ("fake_only_test", "function", 1, "def fake_only_test():"),
        ],
        "longdoc.py": [
            ("longdoc_fn", "function", 3, "def longdoc_fn():"),
        ],
    }
    conn = cdb.init_db(root)
    try:
        for rel, sym_rows in rows.items():
            fpath = str(p / rel)
            st = os.stat(fpath)
            cdb.upsert_file(conn, fpath, st.st_mtime, st.st_size, "python",
                           "test-brief", parser_gen=config.PARSER_GENERATION)
            cdb.insert_symbols(conn, root, fpath, sym_rows, commit=True)
    finally:
        conn.close()
    return root


def test_brief_byte_stable(tmp_path, monkeypatch):
    root = _seed_brief_fixture(tmp_path, monkeypatch)
    from code_map import brief, config
    b1 = brief.build_target_brief(root, "sess-1")
    b2 = brief.build_target_brief(root, "sess-1")
    assert b1 is not None
    assert b1 == b2
    lines = b1.split("\n")
    assert lines[0].startswith("## briefproj")
    # index line carries the generation stamp + freeze label
    idx = [l for l in lines if l.startswith("index: ")]
    assert idx and config.PARSER_GENERATION in idx[0]
    assert "rows · " in idx[0] and "@freeze(sess-1)" in idx[0]
    # layer contract line present (frozen layer disclosure)
    assert any(l.startswith("layer: ") for l in lines)


def test_brief_line_cap(tmp_path, monkeypatch):
    root = _seed_brief_fixture(tmp_path, monkeypatch)
    from code_map import brief
    block = brief.build_target_brief(root, "sess-1")
    assert len(block.split("\n")) <= 8


def test_brief_plugin_entry_line(tmp_path, monkeypatch):
    root = _seed_brief_fixture(tmp_path, monkeypatch)
    from code_map import brief
    block = brief.build_target_brief(root, "sess-1")
    entry = [l for l in block.split("\n") if l.startswith("entry: ")]
    assert len(entry) == 1
    assert "register(ctx)" in entry[0]
    assert "2 tools" in entry[0]


def test_brief_generic_no_entry_line(tmp_path, monkeypatch):
    # Generic project: strip the plugin markers (register + ctx.register_tool)
    root = _seed_brief_fixture(tmp_path, monkeypatch)
    init = Path(root) / "__init__.py"
    init.write_text('"""Generic core module."""\n'
                    "def core_fn():\n    pass\n", encoding="utf-8")
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        cdb.insert_symbols(conn, root, str(init),
                           [("core_fn", "function", 2, "def core_fn():")],
                           commit=True)
    finally:
        conn.close()
    from code_map import brief
    block = brief.build_target_brief(root, "sess-1")
    lines = block.split("\n")
    assert not any(l.startswith("entry: ") for l in lines)
    # the rest of the block still renders
    assert any(l.startswith("modules: ") for l in lines)
    assert any(l.startswith("layer: ") for l in lines)
    assert len(lines) <= 8


def test_brief_heat_excludes_tests_and_tools(tmp_path, monkeypatch):
    root = _seed_brief_fixture(tmp_path, monkeypatch)
    from code_map import brief
    pub = [l for l in brief.build_target_brief(root, "sess-1").split("\n")
           if l.startswith("public: ")]
    joined = " ".join(pub)
    assert "build_search_axes" in joined
    assert "sync_on_write" in joined
    assert "fake_only_test" not in joined


def test_brief_docstring_role_truncated(tmp_path, monkeypatch):
    root = _seed_brief_fixture(tmp_path, monkeypatch)
    from code_map import brief
    mod = [l for l in brief.build_target_brief(root, "sess-1").split("\n")
           if "longdoc" in l]
    assert mod, "longdoc module must appear in the modules line"
    # the 130-char docstring line must be truncated to <= 56 chars of role
    # (+1 ellipsis); the 'longdoc=' prefix is 8 chars
    role_part = [seg for seg in mod[0].split(" · ")
                 if seg.startswith("longdoc=")]
    assert role_part and len(role_part[0]) <= 8 + 56 + 1
    assert role_part[0].endswith("…")  # truncation marker present


def test_render_briefs_cap_and_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh2"))
    _fresh_imports()
    from code_map import db as cdb
    from code_map import rootreg, config

    roots = []
    base_t = time.time()
    for i in range(6):
        proj = tmp_path / f"cap{i}"
        proj.mkdir()
        (proj / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
        r = str(proj)
        rootreg.write_confirmed_root(r)
        conn = cdb.init_db(r)
        try:
            st = os.stat(str(proj / "a.py"))
            cdb.upsert_file(conn, str(proj / "a.py"), st.st_mtime, st.st_size,
                             "python", "cap", parser_gen=config.PARSER_GENERATION)
            cdb.insert_symbols(conn, r, str(proj / "a.py"),
                               [("f", "function", 1, "def f():")], commit=True)
        finally:
            conn.close()
        roots.append(r)
    # stagger db mtimes: cap0 oldest .. cap5 newest
    for i, r in enumerate(roots):
        dbf = config.root_db_path(r)
        os.utime(str(dbf), (base_t + i * 10, base_t + i * 10))

    from code_map import brief
    text = brief.render_briefs("sess-cap")
    assert text.count("## cap") == 3, "only the 3 newest render"
    assert "cap5" in text and "cap4" in text and "cap3" in text
    assert "cap0" not in text
    assert "+3 more" in text


def test_section_content_includes_policy_and_briefs(tmp_path, monkeypatch):
    root = _seed_brief_fixture(tmp_path, monkeypatch)
    from code_map import brief
    out = brief.section_content({"session_id": "s9"})
    from code_map import _ROOT_POLICY_TEXT
    assert _ROOT_POLICY_TEXT in out
    assert "## briefproj" in out


def test_section_content_failsoft(tmp_path, monkeypatch):
    _seed_brief_fixture(tmp_path, monkeypatch)
    import code_map
    from code_map import brief
    real = brief.render_briefs
    monkeypatch.setattr(brief, "render_briefs",
                       lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    out = brief.section_content({"session_id": "s9"})
    from code_map import _ROOT_POLICY_TEXT
    assert out == _ROOT_POLICY_TEXT  # policy only, never a broken section
    monkeypatch.undo()
