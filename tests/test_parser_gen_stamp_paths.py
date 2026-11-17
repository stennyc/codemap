"""F7 (restart-acceptance finding, 2026-11-17): the L2.3c parser-gen
stamp must ride on EVERY write path that upserts a files row — the
state._commit_batch path (host code_map_refresh / L3 build commit
batches) was the one that slipped through 8f2a4b9: it upserted files
rows with the default parser_gen='' , so every host-side refresh
re-manufactured parser_gen_stale rows on the changed files (the
12:49 restart acceptance observed 3 fresh '' rows that only the
host refresh could have written — hook and scanner paths both stamp).

Pin: _commit_batch leaves files rows stamped with the CURRENT
PARSER_GENERATION (never the legacy '')."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_imports() -> None:
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)


def test_commit_batch_stamps_parser_gen(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _fresh_imports()
    from code_map import db as cdb, rootreg, state, config

    root = str(tmp_path / "proj")
    Path(root).mkdir()
    p = Path(root) / "a.py"
    p.write_text("def w():\n    pass\n", encoding="utf-8")
    rootreg.write_confirmed_root(root)
    st = os.stat(str(p))

    conn = cdb.init_db(root)
    try:
        state._commit_batch(conn, root, "probe-build", [
            (str(p), st.st_mtime, st.st_size, "python",
             [("w", "function", 1, "def w():")]),
        ])
        stamp = conn.execute(
            "SELECT parser_gen FROM files WHERE path=?", (str(p),)).fetchone()[0]
        assert stamp == config.PARSER_GENERATION, \
            f"the batch path must stamp the current generation, got {stamp!r}"
        # and a legacy '' row must not be producible by ANY call shape
        assert stamp != "", "a default-arg call shape must not yield the legacy stamp"
    finally:
        conn.close()
