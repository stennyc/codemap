"""Refresh payload test: code_map_refresh returns files_indexed + db_path.

Standing rule (2026-10-11): verify a refresh WITHOUT terminal / log tailing.
The refresh surface itself must carry enough to confirm the index: the
deterministic per-root db path and the count of files currently indexed.

Run per standing rule:
    cd /root/.hermes/plugins && <py3.14> -m pytest code_map/tests/ -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import code_map.tests  # noqa: F401  (sys.path bootstrap, see tests/__init__.py)

import pytest

from code_map.query import code_map_refresh
from code_map.state import refresh_root, confirm_root


def _mk_home(name: str) -> Path:
    h = Path(f"/tmp/cm_refresh_home_{name}")
    h.mkdir(exist_ok=True)
    return h


def _mk_proj(name: str) -> Path:
    p = Path(f"/tmp/cm_refresh_proj_{name}")
    p.mkdir(exist_ok=True)
    return p


def _cleanup(*paths: Path) -> None:
    import shutil
    for p in paths:
        try:
            shutil.rmtree(p)
        except OSError:
            pass


def _seed_files(proj: Path, n: int) -> None:
    for i in range(n):
        (proj / f"f{i}.py").write_text(f"def f{i}(): pass\n")


def test_refresh_returns_files_indexed_and_db_path(monkeypatch):
    """After confirm+refresh, the payload carries files_indexed + db_path,
    and db_path matches the deterministic root_db_path(target)."""
    home = _mk_home("payload")
    monkeypatch.setenv("HERMES_HOME", str(home))
    proj = _mk_proj("payload")
    _seed_files(proj, 3)

    confirm_root("/tmp", proj.name)
    # let the async build settle
    import time
    time.sleep(0.4)

    res = refresh_root(str(proj), "")
    assert res["status"] == "refresh_started"
    # The two fields that close the verification loop without terminal:
    assert "files_indexed" in res, f"missing files_indexed: {res}"
    assert "db_path" in res, f"missing db_path: {res}"

    from code_map.config import root_db_path
    expected_db = str(root_db_path(str(proj)))
    assert res["db_path"] == expected_db, \
        f"db_path {res['db_path']} != expected {expected_db}"

    # files_indexed must be a non-negative int (the count of indexed files;
    # the build is async so it may lag, but the field MUST be present + sane).
    assert isinstance(res["files_indexed"], int) and res["files_indexed"] >= 0

    _cleanup(home, proj)


def test_refresh_tool_surface_returns_enriched_payload(monkeypatch):
    """The tool handler code_map_refresh (what the LLM actually sees) must
    surface files_indexed + db_path in its JSON."""
    home = _mk_home("tool")
    monkeypatch.setenv("HERMES_HOME", str(home))
    proj = _mk_proj("tool")
    _seed_files(proj, 2)

    confirm_root("/tmp", proj.name)
    import time
    time.sleep(0.4)

    out = json.loads(code_map_refresh({"root": "/tmp", "path": proj.name}))
    assert out["status"] == "refresh_started"
    assert "files_indexed" in out, f"tool payload missing files_indexed: {out}"
    assert "db_path" in out, f"tool payload missing db_path: {out}"

    _cleanup(home, proj)


def test_refresh_db_path_deterministic_even_before_build(monkeypatch):
    """db_path is derived from root_db_path(target) and is present even when
    the build hasn't produced a db yet (no file walk, no log tail)."""
    home = _mk_home("pre")
    monkeypatch.setenv("HERMES_HOME", str(home))
    proj = _mk_proj("pre")
    _seed_files(proj, 1)
    confirm_root("/tmp", proj.name)

    # call refresh immediately, before the async build writes the db
    res = refresh_root(str(proj), "")
    from code_map.config import root_db_path
    assert res["db_path"] == str(root_db_path(str(proj)))
    # files_indexed present (may be 0 pre-build) — the point is it's there,
    # so the caller never has to fall back to terminal.
    assert "files_indexed" in res

    _cleanup(home, proj)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
