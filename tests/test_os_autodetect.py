"""OS auto-detection: code_map's path guards must be per-OS, not POSIX-hardcoded.

Owner requirement (2026-11-16): ".hermes is the POSIX home layout; Windows
canon is %LOCALAPPDATA%\\hermes — code_map MUST auto-detect the OS". Audited
holes this file locks down (each RED before the fix):

1. `config.data_dir()` deepest fallback was `Path.home()/".hermes"` — on
   Windows that creates the phantom second tree (today's
   C:\\Users\\user\\.hermes forensics). Now: Windows -> %LOCALAPPDATA%\\hermes.
2. `rootreg._join_target` rejected only `root_abs == os.sep` — a bare
   drive root ("C:\\") sailed through gate 10 on Windows; (root="C:\\",
   path="Windows") would index the whole system tree (382k incident shape).
3. The hook ledger's system-tree guard hardcoded ("/usr","/etc","/opt") —
   dead on Windows (abspath("/usr") == "C:\\usr"), so a read under
   C:\\Windows\\... would log an "index this project?" ledger row.
4. `BLOCKED_ROOT_PREFIXES` was a POSIX-only constant with no Windows twin.

Semantics preserved from the hook docstring (owner 2026-10-18): /root and
/home are DELIBERATELY not guarded (owner projects live under them); the
Windows twin keeps C:\\Users unguarded for the same reason.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from code_map import config
from code_map import rootreg
from code_map.hook import _log_unindexed_read


# ── 1. bare filesystem root, per-OS ─────────────────────────────────────────
def test_bare_fs_root_posix():
    assert config.is_bare_fs_root("/", False) is True
    assert config.is_bare_fs_root("", False) is True
    assert config.is_bare_fs_root("/usr", False) is False
    assert config.is_bare_fs_root("/root", False) is False


def test_bare_fs_root_windows_bare_drive():
    for spell in ("C:\\", "C:/", "C:", "c:\\"):
        assert config.is_bare_fs_root(spell, True) is True, spell
    assert config.is_bare_fs_root("C:\\Users", True) is False
    assert config.is_bare_fs_root("D:\\", True) is True


# ── 2. system-tree guard (gate ⑩ incident shape), per-OS ───────────────────
def test_system_tree_windows():
    assert config.is_system_tree("C:\\Windows\\System32\\foo.py", True) is True
    assert config.is_system_tree("C:\\Program Files\\x\\y.py", True) is True
    assert config.is_system_tree("C:\\Program Files (x86)\\x", True) is True
    assert config.is_system_tree("C:\\ProgramData\\a", True) is True
    assert config.is_system_tree("C:\\", True) is True  # bare drive root
    # case-insensitive (Windows):
    assert config.is_system_tree("c:\\windows\\x", True) is True
    # mixed separators accepted:
    assert config.is_system_tree("C:/Windows/x", True) is True
    # the OWNER workspace is not a system tree — the ledger's core case
    # (indexing AppData\Local\hermes\plugins\filemem) must stay alive:
    assert config.is_system_tree(
        "C:\\Users\\me\\AppData\\Local\\hermes\\plugins", True
    ) is False
    assert config.is_system_tree("C:\\Users\\me\\proj", True) is False


def test_system_tree_posix_unchanged():
    assert config.is_system_tree("/usr/share/x", False) is True
    assert config.is_system_tree("/etc/x", False) is True
    assert config.is_system_tree("/opt/x", False) is True
    assert config.is_system_tree("/", False) is True
    # /root and /home deliberately NOT guarded (owner projects, 2026-10-18):
    assert config.is_system_tree("/home/owner/proj", False) is False
    assert config.is_system_tree("/root/proj", False) is False


# ── 3. gate 10 join: bare drive root / system tree must not pass ───────────
def test_join_target_rejects_bare_drive_root():
    assert rootreg._join_target("C:\\", "Windows") is None
    assert rootreg._join_target("C:", "Users") is None
    assert rootreg._join_target("D:/", "x") is None


def test_join_target_rejects_system_tree(tmp_path):
    assert rootreg._join_target("C:\\Windows", "System32") is None
    import sys
    if sys.platform != "win32":
        assert rootreg._join_target("/usr", "bin") is None
    else:
        # POSIX literals re-root onto the CWD drive on Windows ("C:\\usr" is
        # NOT a Windows system tree) — the Win system dirs are the guard:
        assert rootreg._join_target("C:\\Program Files", "x") is None
        assert rootreg._join_target("C:\\ProgramData", "x") is None


def test_join_target_still_allows_normal_pairs(tmp_path):
    assert rootreg._join_target("C:\\Users\\me\\proj", "src") == "C:\\Users\\me\\proj\\src"
    # platform-native pair (POSIX-style literals re-root onto the CWD drive
    # on Windows — assert with a real tmp path instead):
    assert rootreg._join_target(str(tmp_path), "src") == os.path.join(str(tmp_path), "src")


# ── 4. hook ledger guard follows the host OS ───────────────────────────────
def test_ledger_skips_windows_system_tree(monkeypatch, tmp_path):
    import pytest
    if not config.IS_WINDOWS:
        pytest.skip("Windows-semantics test (the guard binds the host OS flag)")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    _log_unindexed_read(
        "read_file", {"path": "C:\\Windows\\System32\\drivers\\x.py"}, "s1"
    )
    ledger = tmp_path / "hh" / "plugins" / "code_map" / "data" / "_unindexed_read_ledger"
    text = ledger.read_text().strip() if ledger.exists() else ""
    assert "C:\\Windows" not in text, "a Windows system tree must never be offered for indexing"


def test_ledger_still_logs_real_project(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("", encoding="utf-8")
    main = proj / "main.py"
    main.write_text("x = 1\n", encoding="utf-8")
    _log_unindexed_read("read_file", {"path": str(main)}, "s2")
    ledger = tmp_path / "hh" / "plugins" / "code_map" / "data" / "_unindexed_read_ledger"
    text = ledger.read_text()
    assert str(proj) in text, "a real project under the owner workspace must still be offered"


# ── 5. data_dir deepest fallback is OS-aware ───────────────────────────────
def test_data_dir_fallback_windows_uses_localappdata(monkeypatch, tmp_path):
    if not config.IS_WINDOWS:
        import pytest
        pytest.skip("POSIX host")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_constants", None)  # force the deepest fallback
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    d = config.data_dir()
    local = str(tmp_path / "local").lower()
    assert str(d).lower().startswith(local), f"{d} not under LOCALAPPDATA"
    assert ".hermes" not in d.parts, "Windows must never fall back to a POSIX ~/.hermes tree"


# ── 6. static guard: no test module may register confirmed roots without
#    HERMES_HOME isolation (the 2026-11-16 _registry leak, L9-L11/L2) ───────
def test_seed_helpers_isolate_hermes_home():
    import re
    tests_dir = Path(__file__).parent
    offenders = []
    for p in sorted(tests_dir.glob("*.py")):
        src = p.read_text(encoding="utf-8")
        if re.search(r"write_confirmed_root\s*\(", src) and "HERMES_HOME" not in src:
            offenders.append(p.name)
    assert not offenders, (
        f"test modules that register confirmed roots without HERMES_HOME "
        f"isolation (production _registry leak): {offenders}")


def test_data_dir_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "env_home"))
    d = config.data_dir()
    assert str(d) == str(tmp_path / "env_home" / "plugins" / "code_map" / "data")
