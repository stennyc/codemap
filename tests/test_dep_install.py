"""dep_install: auto-install of missing tree-sitter grammar packages.

Owner directive (2026-11-17): "装，而且要添加自动安装，涵盖 code_map 所需
tree-sitter" — the dep venv self-heals its grammar set (registry.
SUPPORTED_LANGUAGES is the complete list; requirements.txt pins the
packages). Pure core (pip-name mapping, missing-diff, argv build, marker
rate-limit) is unit-testable without a network; the I/O wrapper takes an
injected runner so tests never touch the real venvs.

Test isolation: CODE_MAP_AUTO_INSTALL=off is set by tests/conftest.py
before any test imports code_map, so the __init__ wiring cannot
self-heal the real managed venv during a suite run.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["CODE_MAP_AUTO_INSTALL"] = "off"  # belt-and-braces for fresh imports
from code_map import dep_install  # noqa: E402


def _fresh_imports() -> None:
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)


# ── pure core ────────────────────────────────────────────────────────────────

def test_pip_name_for_grammar_modules():
    assert dep_install.pip_name_for("tree_sitter_java") == "tree-sitter-java"
    assert dep_install.pip_name_for("tree_sitter_python") == "tree-sitter-python"
    assert dep_install.pip_name_for("tree_sitter_c") == "tree-sitter-c"


def test_pip_name_for_passthrough():
    # a non-grammar module name passes through (dashes normalized)
    assert dep_install.pip_name_for("filelock") == "filelock"
    assert dep_install.pip_name_for("some_pkg") == "some-pkg"


def test_grammar_packages_covers_registry():
    from code_map import registry
    pkgs = dep_install.grammar_packages()
    assert set(pkgs) == set(registry.SUPPORTED_LANGUAGES)
    for lang, mod in registry.SUPPORTED_LANGUAGES.items():
        assert pkgs[lang] == dep_install.pip_name_for(mod)


def test_grammar_packages_match_requirements_txt():
    """The auto-install set is exactly the grammar set in requirements.txt
    (the owner's '完整列表') — a drift between the two would silently
    install the wrong packages."""
    pkgs = set(dep_install.grammar_packages().values())
    req = Path(__file__).parent.parent / "requirements.txt"
    req_names = {
        ln.split(">=")[0].strip()
        for ln in req.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith("tree-sitter-")
    }
    assert pkgs == req_names, (pkgs, req_names)


def test_missing_packages_diff():
    missing = dep_install.missing_packages(
        ["python", "javascript", "kotlin", "php", "typescript"])
    assert "tree-sitter-java" in missing
    assert "tree-sitter-python" not in missing
    assert len(missing) == 11 - 5


def test_missing_packages_none_missing():
    from code_map import registry
    missing = dep_install.missing_packages(sorted(registry.SUPPORTED_LANGUAGES))
    assert missing == []


def test_build_install_argv_uv():
    argv = dep_install.build_install_argv(
        r"C:\venv\Scripts\python.exe", ["tree-sitter-java"], use_uv=True)
    assert argv[:4] == ["uv", "pip", "install", "--python"]
    assert argv[4] == r"C:\venv\Scripts\python.exe"
    assert argv[5:] == ["tree-sitter-java"]


def test_build_install_argv_pip():
    argv = dep_install.build_install_argv(
        r"C:\venv\Scripts\python.exe",
        ["tree-sitter-c", "tree-sitter-cpp"], use_uv=False)
    assert argv == [r"C:\venv\Scripts\python.exe", "-m", "pip", "install",
                    "tree-sitter-c", "tree-sitter-cpp"]


# ── marker rate-limit (pure) ────────────────────────────────────────────────

def test_marker_due_no_marker():
    assert dep_install.marker_due(None, now=1000.0, missing=["tree-sitter-java"])


def test_marker_due_fresh_same_set():
    marker = {"ts": 900.0, "missing": ["tree-sitter-java"]}
    # now=1000 -> 100s old, well inside 24h, same set -> NOT due
    assert not dep_install.marker_due(marker, now=1000.0,
                                      missing=["tree-sitter-java"])


def test_marker_due_stale():
    marker = {"ts": 0.0, "missing": ["tree-sitter-java"]}
    assert dep_install.marker_due(marker, now=25 * 3600.0,
                                  missing=["tree-sitter-java"])


def test_marker_due_set_changed():
    # same age, but a NEW missing package appeared -> due (re-attempt)
    marker = {"ts": 900.0, "missing": ["tree-sitter-java"]}
    assert dep_install.marker_due(marker, now=1000.0,
                                  missing=["tree-sitter-java", "tree-sitter-go"])


# ── target resolution (pure-ish, tmp HERMES_HOME) ───────────────────────────

def test_resolve_managed_venv_python(tmp_path, monkeypatch):
    hh = tmp_path / "hh"
    venv = hh / "installs" / "k1" / "venv"
    (hh / "installs" / "k1").mkdir(parents=True)
    (venv / ("Scripts" if os.name == "nt" else "bin")).mkdir(parents=True)
    (hh / "installs" / "k1" / "facts.json").write_text(
        json.dumps({"packages": {"venv": {"environment": str(venv)}}}),
        encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hh))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    py = venv / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    py.write_text("")  # existence only
    assert dep_install.resolve_managed_venv_python() == str(py)


def test_resolve_managed_venv_python_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty"))
    assert dep_install.resolve_managed_venv_python() is None


# ── maybe_auto_install (injected runner, tmp HERMES_HOME data dir) ──────────

def _mk_report_env(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CODE_MAP_AUTO_INSTALL", mode)


def _patch_avail(monkeypatch, langs):
    """Pin registry.available_languages so the test is deterministic
    regardless of the host's real venvs (ensure_dep_paths splices the
    managed venv into the test process, so the real probe sees a
    different set)."""
    monkeypatch.setattr(dep_install.registry, "available_languages",
                       lambda: list(langs))
    return sorted(dep_install.missing_packages(langs))


PARTIAL = ["python", "javascript", "kotlin", "php", "typescript"]  # 5 of 11


def test_maybe_auto_install_off(monkeypatch, tmp_path):
    _mk_report_env(monkeypatch, tmp_path, "off")
    rep = dep_install.maybe_auto_install()
    assert rep["attempted"] is False
    assert rep["skipped_reason"] == "CODE_MAP_AUTO_INSTALL=off"


def test_maybe_auto_install_dry_no_runner_call(monkeypatch, tmp_path):
    _mk_report_env(monkeypatch, tmp_path, "dry")
    missing = _patch_avail(monkeypatch, PARTIAL)
    called = []
    rep = dep_install.maybe_auto_install(runner=lambda argv, to: called.append(argv) or (0, ""))
    assert called == []
    assert rep["skipped_reason"] == "dry-run"
    assert rep["argv"]  # the would-run command is reported
    assert rep["missing"] == missing


def test_maybe_auto_install_on_runs_runner_and_writes_marker(
        monkeypatch, tmp_path):
    _mk_report_env(monkeypatch, tmp_path, "on")
    missing = _patch_avail(monkeypatch, PARTIAL)
    calls = []

    def fake_runner(argv, timeout):
        calls.append(list(argv))
        return 0, "installed"

    rep = dep_install.maybe_auto_install(runner=fake_runner, now=5000.0)
    assert rep["attempted"] is True and rep["exit_code"] == 0
    assert calls, "runner was not invoked"
    # marker landed in the ISOLATED data dir
    from code_map.config import data_dir
    marker = Path(data_dir()) / dep_install.MARKER_NAME
    assert marker.is_file()
    rec = json.loads(marker.read_text(encoding="utf-8"))
    assert rec["ts"] == 5000.0
    assert sorted(rec["missing"]) == missing == sorted(rep["missing"])


def test_maybe_auto_install_marker_fresh_skips(monkeypatch, tmp_path):
    _mk_report_env(monkeypatch, tmp_path, "on")
    missing = _patch_avail(monkeypatch, PARTIAL)
    # pre-seed a FRESH marker for exactly the current missing set
    from code_map.config import data_dir
    os.makedirs(data_dir(), exist_ok=True)
    rec = {"ts": 4999.0, "missing": missing, "target": "x", "exit_code": 0}
    Path(data_dir(), dep_install.MARKER_NAME).write_text(
        json.dumps(rec), encoding="utf-8")
    called = []
    rep = dep_install.maybe_auto_install(
        runner=lambda argv, to: called.append(argv) or (0, ""), now=5000.0)
    assert called == []
    assert rep["skipped_reason"] == "marker fresh + same missing set"


def test_maybe_auto_install_all_present(monkeypatch, tmp_path):
    _mk_report_env(monkeypatch, tmp_path, "on")
    _patch_avail(monkeypatch, sorted(dep_install.registry.SUPPORTED_LANGUAGES))
    rep = dep_install.maybe_auto_install()
    assert rep["attempted"] is False
    assert rep["skipped_reason"] == "none missing"


def test_maybe_auto_install_fail_soft(monkeypatch, tmp_path):
    _mk_report_env(monkeypatch, tmp_path, "on")
    _patch_avail(monkeypatch, PARTIAL)
    rep = dep_install.maybe_auto_install(
        runner=lambda argv, to: (_ for _ in ()).throw(OSError("boom")),
        now=5000.0)
    # an exploded runner is fail-soft: reported (exit 1), not raised
    assert rep["attempted"] is True
    assert rep["exit_code"] == 1
    assert "boom" in (rep["error_tail"] or "")


def test_init_wiring_exists():
    """The __init__ bootstrap calls maybe_auto_install fail-soft (owner
    directive: auto-install covers the code_map runtime)."""
    import code_map
    src = Path(code_map.__file__).read_text(encoding="utf-8")
    assert "_maybe_auto_install_grammars" in src
    assert "maybe_auto_install" in src
