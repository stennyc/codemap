"""Gate 10 v2 entry-legality tests: confirm_root takes a (root, path) pair.

Owner's rule (2026-10-11 v2): the gate is a CONCATENATION rule, not a
blacklist. Both fields are mandatory; the effective target is
normpath(join(root, path)); bare '/' as the boundary root is refused;
an absolute path or a ../-escaping path is refused. What used to be
the "blocked prefix" list (/root, /home, /usr, /etc, /opt as REFUSED
roots) is now INVERTED: those prefixes are perfectly legal BOUNDARIES
as long as a non-empty relative path pins the target inside them.

Run per standing rule:
    cd /root/.hermes/plugins && <py3.14> -m pytest code_map/tests/ -q
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import code_map.tests  # noqa: F401  (sys.path bootstrap, see tests/__init__.py)

import pytest

from code_map import rootreg
from code_map.state import confirm_root


def test_gate10_root_empty_path_must_be_present(tmp_path, monkeypatch):
    """root='/root', path='' -> invalid: a whole-boundary index is the
    382k-file incident; it can never pass the gate."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    res = confirm_root("/root", "")
    assert res["status"] == "invalid", res


@pytest.mark.parametrize("root", ["/root", "/home", "/usr", "/etc", "/opt"])
def test_gate10_boundary_root_always_legal_with_path(root, tmp_path, monkeypatch):
    """Every old 'blocked' prefix is now a LEGAL boundary when a
    non-empty relative path pins the target inside it (v2 inversion).
    (Bare '/' is NOT in this list: it is refused outright — see
    test_gate10_bare_slash_root_refused.)

    HERMES_HOME is redirected so the confirm does NOT pollute the
    production data/ dir; the gate is syntactic, no project dir needed."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    res = confirm_root(root, "some/real/project")
    assert res["status"] == "build_started", f"{root}: {res}"
    assert res["root"] == root + "/some/real/project"


def test_gate10_bare_slash_root_refused():
    """root='/' with ANY path is refused: whole-filesystem scan."""
    res = confirm_root("/", "root/.hermes/plugins/filemem")
    assert res["status"] == "invalid", res


def test_gate10_absolute_path_refused():
    """path must be RELATIVE to root; an absolute path defines no
    boundary and is refused."""
    res = confirm_root("/root", "/root/.hermes/plugins/filemem")
    assert res["status"] == "invalid", res


def test_gate10_traversal_escape_refused():
    """A path that normpaths out of the boundary is refused."""
    res = confirm_root("/root", ".hermes/plugins/filemem/../../../../etc")
    assert res["status"] == "invalid", res


def test_gate10_empty_root_refused():
    """root='' with any path is refused (no boundary)."""
    res = confirm_root("", ".hermes/plugins/filemem")
    assert res["status"] == "invalid", res


def test_legal_pair_starts_build_and_registers(tmp_path, monkeypatch):
    """A passing pair registers the JOINED target (not the boundary)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    proj = Path("/tmp/cm_gate10v2_legal")
    shutil.rmtree(proj, ignore_errors=True)
    proj.mkdir(parents=True)
    (proj / "x.py").write_text("def f(): pass\n")
    try:
        res = confirm_root("/tmp", "cm_gate10v2_legal")
        assert res["status"] == "build_started", res
        assert res["root"] == str(proj)
        assert rootreg.is_confirmed_root(str(proj)) is True
        # the boundary itself is NOT the registered target:
        assert rootreg.is_confirmed_root("/tmp") is False
    finally:
        shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
