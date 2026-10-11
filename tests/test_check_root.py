"""2B code_map_check_root tests: dry-run default, confirm-gated build, jsonl audit.

Run per standing rule:
    cd /root/.hermes/plugins && <py3.14> -m pytest code_map/tests/ -q

Design (borrowed from filemem sweep_unconfirmed.py protocol, not its tool shell):
  - dry-run is the DEFAULT: reports what WOULD be indexed, writes nothing
  - confirm (yes=True) lifts to actually call state.confirm_root
  - every confirm success appends ONE jsonl audit line to data/confirm_root_log.jsonl
    (recoverable trail: who built which root, when, how many files)
  - gate 10 v2 (root+path both mandatory, joined target) applies to the confirm path

v2 (2026-10-11): the check tool now takes a (root, path) pair — root is
a boundary prefix, path the relative sub-path. Test dirs live under
/tmp with a shared parent so the pair shape is exercisable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from code_map.query import code_map_check_root


def _mk_proj(dir_name: str) -> Path:
    p = Path(f"/tmp/cm_check_root_{dir_name}")
    p.mkdir(exist_ok=True)
    return p


def _mk_home(dir_name: str) -> Path:
    h = Path(f"/tmp/cm_check_home_{dir_name}")
    h.mkdir(exist_ok=True)
    return h


def _audit_lines(home: Path) -> list:
    p = home / "plugins" / "code_map" / "data" / "confirm_root_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _cleanup(*paths: Path) -> None:
    import shutil
    for p in paths:
        try:
            shutil.rmtree(p)
        except OSError:
            pass


def test_check_root_default_is_dry_run(monkeypatch):
    """Default call (no yes) must report preview stats and NOT build / write audit."""
    home = _mk_home("dryrun")
    monkeypatch.setenv("HERMES_HOME", str(home))
    proj = _mk_proj("dryrun")
    (proj / "a.py").write_text("def fa(): pass\n")
    (proj / "b.py").write_text("class Cb:\n    pass\n")

    res = json.loads(code_map_check_root(
        {"root": "/tmp", "path": proj.name}))
    # dry-run: reports count, does NOT start a build, does NOT write audit
    assert res["dry_run"] is True
    assert res["files_indexable"] == 2
    # no audit line written yet
    audit = _audit_lines(home)
    assert audit == [], f"dry-run must not write audit, got {audit}"
    # no db created for this (fresh) target
    from code_map.config import root_db_path
    assert not root_db_path(str(proj)).exists(), "dry-run must not build a db"

    _cleanup(home, proj)


def test_check_root_yes_writes_audit_and_builds(monkeypatch):
    """yes=True must call confirm_root and append exactly one audit line."""
    _cleanup(_mk_home("apply"), _mk_proj("apply"))
    home = _mk_home("apply")
    monkeypatch.setenv("HERMES_HOME", str(home))
    proj = _mk_proj("apply")
    (proj / "a.py").write_text("def fa(): pass\n")

    res = json.loads(code_map_check_root(
        {"root": "/tmp", "path": proj.name, "yes": True}))
    assert res["dry_run"] is False
    assert res["status"] == "build_started"

    audit = _audit_lines(home)
    assert len(audit) == 1, f"expect exactly 1 audit line, got {audit}"
    line = audit[0]
    assert line["action"] == "confirm_root"
    assert line["root"] == str(proj)
    assert line["files_indexed"] == 1
    assert "ts" in line and "db_path" in line

    _cleanup(home, proj)


def test_check_root_gate_invalid_pair_refused_even_with_yes(monkeypatch):
    """gate v2: a root-only pair (empty path) must be refused."""
    home = _mk_home("blocked")
    monkeypatch.setenv("HERMES_HOME", str(home))
    res = json.loads(code_map_check_root(
        {"root": "/root", "path": "", "yes": True}))
    assert res["status"] == "invalid"
    # no audit line for a refused build
    audit = _audit_lines(home)
    assert audit == [], "refused pair must not write audit"

    _cleanup(home)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
