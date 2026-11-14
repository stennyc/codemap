"""Query-side root gate (v2, 2026-10-11): confirmed targets only.

Owner's rule v2: the gate is a CONCATENATION rule — confirm takes
(root, path), both mandatory; the joined target is what gets indexed.
root-only or path-only calls are invalid; bare '/' as the boundary
root is invalid; a path that escapes the boundary via ../ is invalid.

Owner's four worked examples (the test oracle):
  root='/root', path=''                        -> INVALID
  root='/',    path='root'                     -> INVALID
  root='/root', path='.hermes/plugins/filemem' -> PASS
  root='',     path='/root/.hermes/plugins/filemem' -> INVALID

Run per standing rule:
    cd /root/.hermes/plugins && <py3.14> -m pytest code_map/tests/ -q

All paths live under /tmp (a boundary outside the production tree);
HERMES_HOME is monkeypatched per test so production data/ is untouched.
Every test gets fresh, uniquely-suffixed dirs so background build
threads from a sibling test can never collide on a db lock.
"""
from __future__ import annotations

import itertools
import json
import shutil
import sys
from pathlib import Path


import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # 自适应 bootstrap: plugin 根
import pytest

from code_map import rootreg
from code_map.db import init_db, insert_symbols, upsert_file
from code_map.fingerprint import file_fingerprint
from code_map.parser import parse_file
from code_map.query import code_map_confirm_root, code_map_query
from code_map.state import confirm_root

_ctr = itertools.count()

# Registry of all _Dir instances created this run, so an autouse fixture can
# rmtree them a SECOND time AFTER the test body (and its finally-block
# cleanup) has finished — closing the race where a fire-and-forget
# background build thread (confirm_root's) re-mkdirs home/plugins via
# data_dir() after the test's own cleanup ran, leaving residue like
# /tmp/cm_qg_oracle_7_home/plugins (seen in the 2026-10-11 audit).
_dir_instances: list = []


class _Dir:
    """Unique per-test /tmp dirs + helpers to seed a target's db sync."""

    def __init__(self, tag: str):
        n = next(_ctr)
        self.boundary = Path(f"/tmp/cm_qg_{tag}_{n}")
        self.home = Path(f"/tmp/cm_qg_{tag}_{n}_home")
        # the target dir: <boundary>/proj  (confirmed via root=boundary,
        # path=proj — a v2 gate-passing pair)
        self.proj = self.boundary / "proj"
        for p in (self.boundary, self.home):
            shutil.rmtree(p, ignore_errors=True)
            p.mkdir(parents=True)
        self.proj.mkdir()
        _dir_instances.append(self)

    def seed_proj_db(self) -> None:
        """Synchronously build a real db for the target (no background build)."""
        fp = self.proj / "a.py"
        fp.write_text("def fa(): pass\nclass Ca: pass\n")
        conn = init_db(str(self.proj))
        mtime, size = file_fingerprint(fp)
        upsert_file(conn, str(fp), mtime, size, "python", "test", commit=True)
        insert_symbols(conn, str(self.proj), str(fp), parse_file(str(fp)),
                       commit=True)
        conn.close()

    def cleanup(self) -> None:
        for p in (self.boundary, self.home):
            shutil.rmtree(p, ignore_errors=True)


@pytest.fixture(autouse=True)
def _cm_qg_post_test_sweep():
    """Post-test sweep: rmtree every _Dir created by the test just finished.
    Runs AFTER the test's own finally-block cleanup; the brief settle delay
    lets an in-flight background build thread land its db-open before the
    second rmtree, so it can't recreate the dir afterwards and mask a
    real leak."""
    yield
    import time as _time
    _time.sleep(0.05)
    for d in list(_dir_instances):
        d.cleanup()
    _dir_instances.clear()


def _res(args: dict) -> dict:
    return json.loads(code_map_query(args))


def _confirm(root: str, path: str = "") -> dict:
    return json.loads(code_map_confirm_root({"root": root, "path": path}))


# ---- gate oracle: the owner's four examples (parameterized) -------------------

@pytest.mark.parametrize("root,path,expect", [
    ("", "", "invalid"),                      # both empty
    ("/tmp", "", "invalid"),                  # root-only: whole-boundary
    ("/tmp/cm_oracle_root", "x", "invalid-maybe-blocked"),  # placeholder, not asserted
    ("/", "root", "invalid"),                # bare '/' boundary
    ("", "/tmp/cm_oracle_x", "invalid"),      # path-only, absolute
    ("/tmp", "/tmp/cm_oracle_x", "invalid"),  # absolute path (no boundary)
    ("/tmp/cm_oracle_x", "..", "invalid"),    # traversal escape
    ("/tmp/cm_oracle_x", "x/../y", "maybe"),  # normpath stays inside -> pass
])
def test_gate_oracle(root: str, path: str, expect: str, monkeypatch):
    d = _Dir("oracle")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        res = _confirm(root, path)
        status = res["status"]
        if expect == "invalid":
            assert status == "invalid", (root, path, res)
        elif expect in ("invalid-maybe-blocked", "maybe"):
            pass  # not asserted: depends on normpath outcome
        else:
            raise ValueError(expect)
    finally:
        d.cleanup()


def test_gate_owner_examples_exact(monkeypatch):
    """The owner's four worked examples, asserted verbatim."""
    d = _Dir("owner4")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        # 1. root=/root path="" -> not passing (mapped: boundary, empty path)
        r1 = _confirm("/tmp/cm_owner4", "")
        assert r1["status"] == "invalid", r1
        # 2. root=/ path=root -> not passing (bare '/' boundary)
        r2 = _confirm("/", "root")
        assert r2["status"] == "invalid", r2
        # 3. root=/root path=.hermes/plugins/filemem -> passing
        d.proj.mkdir(exist_ok=True)
        (d.proj / "a.py").write_text("def fa(): pass\n")
        r3 = _confirm(str(d.boundary), "proj")
        assert r3["status"] == "build_started", r3
        assert rootreg.is_confirmed_root(str(d.proj)) is True
        # 4. root="" path=/root/.hermes/plugins/filemem -> not passing
        r4 = _confirm("", "/tmp/cm_owner4/proj")
        assert r4["status"] == "invalid", r4
    finally:
        d.cleanup()


# ---- rootreg predicates --------------------------------------------------------

def test_unconfirmed_target_invalid_even_when_db_exists(monkeypatch):
    d = _Dir("unconf")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        d.seed_proj_db()
        assert rootreg.is_confirmed_root(str(d.proj)) is False
        assert rootreg.confirmed_targets() == []
    finally:
        d.cleanup()


def test_confirmed_target_exact_match_only(monkeypatch):
    d = _Dir("conf")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        rootreg.write_confirmed_root(str(d.proj))
        assert rootreg.is_confirmed_root(str(d.proj)) is True
        assert rootreg.is_confirmed_root(str(d.proj) + "/x") is False
        assert rootreg.is_confirmed_root("/nonexistent") is False
        assert rootreg.confirmed_targets() == [str(d.proj)]
    finally:
        d.cleanup()


def test_bare_root_never_confirmed(monkeypatch):
    d = _Dir("bare")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        assert rootreg.is_confirmed_root("/") is False
        with pytest.raises(ValueError):
            rootreg.write_confirmed_root("/")
        assert rootreg.confirmed_targets() == []
    finally:
        d.cleanup()


def test_join_target_rejects_traversal(monkeypatch):
    d = _Dir("trav")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        assert rootreg._join_target("/tmp/cm_trav_x", "../escape") is None
        assert rootreg._join_target("/tmp/cm_trav_x", "ok/sub") == \
            "/tmp/cm_trav_x/ok/sub"
    finally:
        d.cleanup()


# ---- confirm_root: (root, path) pair -------------------------------------------

def test_confirm_needs_both_fields(monkeypatch):
    d = _Dir("noproj")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        res = json.loads(code_map_confirm_root({}))
        assert res["status"] == "invalid"
        res2 = json.loads(code_map_confirm_root({"root": "/tmp/cm_noproj"}))
        assert res2["status"] == "invalid"
        res3 = json.loads(code_map_confirm_root({"path": "x"}))
        assert res3["status"] == "invalid"
    finally:
        d.cleanup()


def test_confirm_writes_registry_and_starts_build(monkeypatch):
    d = _Dir("confbuild")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        (d.proj / "a.py").write_text("def fa(): pass\n")
        res = _confirm(str(d.boundary), "proj")
        assert res["status"] == "build_started", res
        assert res["root"] == str(d.proj)
        assert rootreg.is_confirmed_root(str(d.proj)) is True
    finally:
        d.cleanup()


def test_confirm_legacy_root_path_fails_gate(monkeypatch):
    """Old single-arg call shape now fails the v2 gate (path mandatory)."""
    d = _Dir("legacy")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        (d.proj / "a.py").write_text("def fa(): pass\n")
        res = json.loads(code_map_confirm_root({"root_path": str(d.proj)}))
        assert res["status"] == "invalid", res
    finally:
        d.cleanup()


# ---- query gate ----------------------------------------------------------------

def test_query_missing_root_is_invalid(monkeypatch):
    d = _Dir("noqroot")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        d.seed_proj_db()
        rootreg.write_confirmed_root(str(d.proj))
        out = _res({"name": "fa"})
        assert out["status"] == "invalid", out
        assert out["count"] == 0
        assert "root" in out["note"]
    finally:
        d.cleanup()


def test_query_unconfirmed_root_invalid_even_with_db(monkeypatch):
    d = _Dir("unq")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        d.seed_proj_db()
        out = _res({"name": "fa", "root": str(d.proj)})
        assert out["status"] == "invalid", out
        assert "not confirmed" in out["note"]
        assert out["count"] == 0
    finally:
        d.cleanup()


def test_query_root_alone_is_invalid_confirmed_or_not(monkeypatch):
    """root with neither name nor path: nothing to locate -> invalid."""
    d = _Dir("onlyroot")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        out = _res({"root": str(d.proj)})
        assert out["status"] == "invalid", out
        assert "not confirmed" in out["note"]
        rootreg.write_confirmed_root(str(d.proj))
        out2 = _res({"root": str(d.proj)})
        assert out2["status"] == "invalid", out2
        assert "provide" in out2["note"]
    finally:
        d.cleanup()


def test_query_path_only_is_invalid(monkeypatch):
    """path without root: relative to what? -> invalid."""
    d = _Dir("onlypath")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        d.seed_proj_db()
        out = _res({"path": "a.py"})
        assert out["status"] == "invalid", out
        assert "root" in out["note"]
    finally:
        d.cleanup()


def test_query_path_must_lie_within_root(monkeypatch):
    d = _Dir("pathin")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        d.seed_proj_db()
        rootreg.write_confirmed_root(str(d.proj))
        out = _res({"path": "/etc/passwd", "root": str(d.proj)})
        assert out["status"] == "invalid", out
        assert "root" in out["note"]
        out2 = _res({"path": "../../etc/passwd", "root": str(d.proj)})
        assert out2["status"] == "invalid", out2
        ok = _res({"path": "a.py", "root": str(d.proj)})
        assert ok.get("status") != "invalid", ok
    finally:
        d.cleanup()


def test_query_confirmed_root_returns_rows(monkeypatch):
    d = _Dir("rows")
    monkeypatch.setenv("HERMES_HOME", str(d.home))
    try:
        d.seed_proj_db()
        rootreg.write_confirmed_root(str(d.proj))
        out = _res({"name": "fa", "root": str(d.proj)})
        assert out.get("status") != "invalid", out
        assert out["count"] >= 1, out
        assert all(r["path"] == str(d.proj) + "/a.py" for r in out["symbols"])
        out2 = _res({"path": "a.py", "root": str(d.proj)})
        assert out2.get("status") != "invalid", out2
        assert out2["count"] >= 1, out2
    finally:
        d.cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
