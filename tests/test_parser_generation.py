"""L2.3c: parser-generation stamp — cross-generation phantom rows.

The 2026-11-17 L2.7 campaign (benchmarks.md) quantified the failure class:
a 18,776-line `__init__.py` carried 55 PHANTOM `min_score` kwarg rows
(index 131 vs AST ground truth 76) written by an earlier parser/grammar
generation; the incremental pipeline (fingerprint = source mtime) cannot
see a parser change, and `staleness=fresh` reported the db as current.
L2.3c stamps every files-table row with the PARSER_GENERATION that wrote
it: a generation mismatch forces a re-parse (scanner), surfaces as
`staleness="parser_gen_stale"` (queries), and a no-grammar hook reindex
leaves the files row untouched (no fake-fresh fingerprint).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_imports() -> None:
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)


def _seed_gen_fixture(tmp_path, monkeypatch) -> str:
    """HERMES_HOME-isolated 2-file python project (confirmed root)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _fresh_imports()
    from code_map import db as cdb
    from code_map import rootreg

    proj = tmp_path / "genproj"
    proj.mkdir()
    (proj / "core.py").write_text("def alpha(x=1):\n    return x\n",
                                  encoding="utf-8")
    (proj / "other.py").write_text("def beta():\n    return 0\n",
                                   encoding="utf-8")
    root = str(proj)
    rootreg.write_confirmed_root(root)

    conn = cdb.init_db(root)
    try:
        for fname in ("core.py", "other.py"):
            p = proj / fname
            st = os.stat(str(p))
            cdb.upsert_file(conn, str(p), st.st_mtime, st.st_size, "python",
                            "t", commit=False)
            cdb.insert_symbols(conn, root, str(p), [
                ("alpha" if fname == "core.py" else "beta", "function", 1,
                 "def ...(")], commit=False)
        conn.commit()
    finally:
        conn.close()
    return root


def test_files_schema_carries_parser_gen(tmp_path, monkeypatch):
    root = _seed_gen_fixture(tmp_path, monkeypatch)
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(files)")]
        assert "parser_gen" in cols, cols
        cdb.upsert_file(conn, str(Path(root) / "core.py"), 1.0, 20, "python",
                        "t", parser_gen="g1")
        row = conn.execute("SELECT parser_gen FROM files WHERE path=?",
                           (str(Path(root) / "core.py"),)).fetchone()
        assert row[0] == "g1"
    finally:
        conn.close()


def test_legacy_db_gets_alter_backfill(tmp_path, monkeypatch):
    """A pre-L2.3c db (files table without the column) is migrated in place:
    the column appears, legacy rows read back as '' (unknown generation)."""
    root = _seed_gen_fixture(tmp_path, monkeypatch)
    import sqlite3
    dbp = Path(os.environ["HERMES_HOME"])
    # rebuild the files table WITHOUT parser_gen to simulate the legacy shape
    from code_map import config
    dbfile = config.root_db_path(root)
    conn = sqlite3.connect(str(dbfile))
    conn.execute("ALTER TABLE files DROP COLUMN parser_gen")
    conn.execute("DELETE FROM files")
    conn.execute("INSERT INTO files(path, mtime, size, lang, build_id, "
                 "fingerprint_epoch) VALUES(?, 1, 10, 'python', 'x', 0)",
                 (str(Path(root) / "core.py"),))
    conn.commit()
    conn.close()
    _fresh_imports()
    from code_map import db as cdb
    conn = cdb.init_db(root)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(files)")]
        assert "parser_gen" in cols, "legacy db must be migrated in place"
        row = conn.execute("SELECT parser_gen FROM files WHERE path=?",
                           (str(Path(root) / "core.py"),)).fetchone()
        assert row[0] == ""  # unknown generation -> re-parse on next build
    finally:
        conn.close()


def test_scanner_reparses_on_generation_mismatch(tmp_path, monkeypatch):
    """THE L2.7 regression: file content UNCHANGED (fingerprint equal) but
    the stamping generation is stale -> the scanner must re-parse + re-stamp
    (pre-L2.3c it skipped on mtime equality and the old rows persisted —
    the 55-phantom-row incident)."""
    root = _seed_gen_fixture(tmp_path, monkeypatch)
    _fresh_imports()
    from code_map import config, db as cdb
    from code_map.scanner import BuildScanner

    # generation A: the db was built under the old parser
    monkeypatch.setattr(config, "PARSER_GENERATION", "A")
    BuildScanner(root, "genA").run()

    # generation B arrives; the file's CONTENT is untouched (mtime unchanged)
    monkeypatch.setattr(config, "PARSER_GENERATION", "B")
    BuildScanner(root, "genB").run()

    conn = cdb.init_db(root)
    try:
        gens = dict((r[0].split(os.sep)[-1], r[1]) for r in conn.execute(
            "SELECT path, parser_gen FROM files").fetchall())
        assert gens.get("core.py") == "B", gens
        assert gens.get("other.py") == "B", gens
    finally:
        conn.close()


def test_staleness_reports_gen_stale(tmp_path, monkeypatch):
    root = _seed_gen_fixture(tmp_path, monkeypatch)
    _fresh_imports()
    from code_map import config, db as cdb
    from code_map.query import _staleness

    monkeypatch.setattr(config, "PARSER_GENERATION", "A")
    conn = cdb.init_db(root)
    try:
        cdb.upsert_file(conn, str(Path(root) / "core.py"),
                        os.stat(str(Path(root) / "core.py")).st_mtime,
                        20, "python", "t", parser_gen="A")
        cdb.upsert_file(conn, str(Path(root) / "other.py"),
                        os.stat(str(Path(root) / "other.py")).st_mtime,
                        20, "python", "t", parser_gen="A")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(config, "PARSER_GENERATION", "B")
    _fresh_imports()
    import code_map.query as qm
    conn = cdb.init_db(root)
    try:
        assert qm._staleness(root, conn) == "parser_gen_stale"
    finally:
        conn.close()

    # all rows re-stamped to the current generation -> fresh again
    monkeypatch.setattr(config, "PARSER_GENERATION", "B")
    from code_map.scanner import BuildScanner
    BuildScanner(root, "genB").run()
    _fresh_imports()
    import code_map.query as qm2
    conn = cdb.init_db(root)
    try:
        assert qm2._staleness(root, conn) == "fresh"
    finally:
        conn.close()


def test_gen_stale_payload_note(tmp_path, monkeypatch):
    """The query payload discloses the gen-stale state so a consumer does
    not read stale-generation rows as current (the 131-vs-76 misread)."""
    root = _seed_gen_fixture(tmp_path, monkeypatch)
    _fresh_imports()
    from code_map import config, db as cdb
    config.PARSER_GENERATION = "A"
    conn = cdb.init_db(root)
    try:
        cdb.upsert_file(conn, str(Path(root) / "core.py"),
                        os.stat(str(Path(root) / "core.py")).st_mtime,
                        20, "python", "t", parser_gen="A")
        conn.commit()
    finally:
        conn.close()
    _fresh_imports()
    config.PARSER_GENERATION = "B"
    from code_map import query
    out = json.loads(query.code_map_query({"root": root, "name": "alpha"}))
    assert out.get("staleness") == "parser_gen_stale"
    blob = json.dumps(out)
    assert "parser generation" in blob, "the payload must disclose the state"
