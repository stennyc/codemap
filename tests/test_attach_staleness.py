"""L2.8h: attach-block generation + staleness stamp (TDD).

Every attach block ends with one index-stamp line:
  index: gen <PARSER_GENERATION> · <rows> rows · fresh|partial|parser_gen_stale
or, when the target is too big for a per-attach disk walk (Q3 cap):
  index: gen <PARSER_GENERATION> · <rows> rows · unknown(large target)
Old blocks stay in conversation history (append-only); each block carries
its OWN stamp so the model can order them — the newest stamp is
authoritative. No retraction machinery (that is the host's context, not
the plugin's).
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_imports() -> None:
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)


def _seed_attach_fixture(tmp_path, monkeypatch) -> str:
    """Minimal confirmed target with a fully-stamped index (clean -> fresh)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _fresh_imports()
    from code_map import db as cdb
    from code_map import rootreg, config

    proj = tmp_path / "attproj"
    proj.mkdir()
    (proj / "core.py").write_text(
        '"""Core module."""\n'
        "def core_fn(a):\n    pass\n", encoding="utf-8")
    root = str(proj)
    rootreg.write_confirmed_root(root)
    conn = cdb.init_db(root)
    try:
        st = os.stat(str(proj / "core.py"))
        cdb.upsert_file(conn, str(proj / "core.py"), st.st_mtime, st.st_size,
                        "python", "test-attach",
                        parser_gen=config.PARSER_GENERATION)
        cdb.insert_symbols(conn, root, str(proj / "core.py"),
                           [("core_fn", "function", 2, "def core_fn(a):"),
                            ("a", "parameter", 2, "def core_fn(a):")],
                           commit=True)
    finally:
        conn.close()
    return root


def _stamp_line(attach_out: str) -> str:
    lines = attach_out.split("\n")
    stamps = [l for l in lines if l.startswith("index: gen ")]
    assert stamps, f"no index-stamp line in attach block:\n{attach_out}"
    return stamps[-1]


def test_attach_stamp_fresh(tmp_path, monkeypatch):
    root = _seed_attach_fixture(tmp_path, monkeypatch)
    from code_map import hook, config
    out = hook._attach_symbols(root, str(Path(root) / "core.py"))
    assert out is not None
    stamp = _stamp_line(out)
    assert config.PARSER_GENERATION in stamp
    assert re.search(r"\d+ rows · fresh$", stamp), stamp


def test_attach_stamp_partial_on_disk_drift(tmp_path, monkeypatch):
    root = _seed_attach_fixture(tmp_path, monkeypatch)
    # an UNINDEXED new file on disk -> the fingerprint walk reports drift
    (Path(root) / "new_mod.py").write_text("def new_fn():\n    pass\n",
                                           encoding="utf-8")
    from code_map import hook
    out = hook._attach_symbols(root, str(Path(root) / "core.py"))
    assert out is not None
    assert "· partial" in _stamp_line(out)


def test_attach_stamp_unknown_large_target(tmp_path, monkeypatch):
    root = _seed_attach_fixture(tmp_path, monkeypatch)
    from code_map import hook, config
    import code_map.query as q

    monkeypatch.setattr(config, "ATTACH_STALENESS_WALK_MAX_FILES", 0)
    walk_spy = []

    def _spy(*a, **k):
        walk_spy.append(1)
        raise AssertionError("walk must be skipped for large targets")

    monkeypatch.setattr(q, "on_disk_files", _spy)
    try:
        out = hook._attach_symbols(root, str(Path(root) / "core.py"))
        assert out is not None
        assert "unknown(large target)" in _stamp_line(out)
        assert walk_spy == []  # the disk walk was never invoked
    finally:
        monkeypatch.undo()


def test_attach_block_without_db_returns_none(tmp_path, monkeypatch):
    # no confirmed root / no db -> no block, no stamp (unchanged behavior)
    from code_map import hook
    assert hook._attach_symbols(str(tmp_path / "nope"),
                                str(tmp_path / "nope" / "x.py")) is None
