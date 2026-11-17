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


# ── 7. db LIKE prefixes must use the host OS path separator ────────────────
# The files table stores os.walk paths (native separators: "\\" on Windows,
# "/" on POSIX). `diff_indexed` / `stale_paths` filtered rows with a POSIX
# forward-slash prefix (`f"{root}/%"`) — on Windows that LIKE matched ZERO
# rows, so every query falsely reported staleness='partial' and deleted
# files were never reported as removed. Each test below is RED on a
# Windows host before the os.sep fix.
def _build_indexed_root(tmp_path, monkeypatch) -> str:
    """Build a 2-file index under a HERMES_HOME-isolated tmp home."""
    import time
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)
    import importlib
    from code_map import fingerprint as fp, db as cdb
    importlib.reload(fp)
    importlib.reload(cdb)
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")
    (proj / "sub" / "b.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    root = str(proj)
    # index by upserting the files exactly as the scanner stores them
    # (native-separator absolute paths from os.walk):
    import os as _os
    on_disk = fp.on_disk_files(root)
    conn = cdb.init_db(root)
    try:
        for p in sorted(on_disk):
            st = _os.stat(p)
            cdb.upsert_file(conn, p, st.st_mtime, st.st_size, "python",
                            build_id="t", commit=False)
            cdb.insert_symbols(conn, root, p, [], commit=False)
        conn.commit()
    finally:
        conn.close()
    return root


def test_diff_indexed_clean_root_reports_no_delta(tmp_path, monkeypatch):
    import os
    root = _build_indexed_root(tmp_path, monkeypatch)
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)
    from code_map import fingerprint as fp
    on_disk = fp.on_disk_files(root)
    added, removed = fp.diff_indexed(root, on_disk)
    assert added == set(), f"fully-indexed root must have no 'added'; got {len(added)}: {sorted(added)[:3]}"
    assert removed == set(), f"no files deleted; got removed {sorted(removed)[:3]}"


def test_stale_paths_reports_on_disk_deletion(tmp_path, monkeypatch):
    import os
    root = _build_indexed_root(tmp_path, monkeypatch)
    os.remove(os.path.join(root, "a.py"))
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)
    from code_map import db as cdb, fingerprint as fp
    fp.invalidate_cache(root)  # on_disk_files caches with a TTL: force re-walk
    conn = cdb.init_db(root)
    try:
        remaining = fp.on_disk_files(root)
        gone = cdb.stale_paths(conn, root, remaining)
    finally:
        conn.close()
    assert gone == {os.path.join(root, "a.py")}, (
        f"deleted file must surface in stale_paths; got {sorted(gone)[:3]}"
    )


# ── 8. transient-dir never-log guard (owner 2026-11-17 "临时目录永不再问") ─
def test_is_transient_dir_windows():
    # transient markers anywhere on the path veto indexing (case-insensitive
    # on Windows, mirroring is_system_tree):
    for spell in (
        "C:\\Users\\me\\AppData\\Local\\hermes\\profiles\\coder\\cache\\scratch",
        "C:\\Users\\me\\AppData\\Local\\hermes\\profiles\\coder\\cache\\spillover",
        "C:\\Users\\me\\AppData\\Local\\hermes\\filemem\\handoffs",
        "C:\\Users\\me\\proj\\__pycache__",
        "C:\\Users\\me\\x\\CACHE",          # case-insensitive on Windows
        "C:/Users/me/.venv/cache",          # mixed separators
        "C:\\Users\\me\\AppData\\Local\\hermes\\profiles\\coder\\skills\\"
        "autonomous-ai-agents\\hermes-file-memory-plugin",  # owner never-propose skill dir
    ):
        assert config.is_transient_dir(spell, True) is True, spell
    # a real owner source project / plugin is NOT transient:
    assert config.is_transient_dir("C:\\Users\\me\\proj\\src", True) is False
    assert config.is_transient_dir(
        "C:\\Users\\me\\AppData\\Local\\hermes\\plugins\\kbs", True) is False


def test_is_transient_dir_posix():
    for spell in ("/home/me/.cache/x", "/home/me/proj/cache",
                  "/tmp/scratch", "/var/lib/hermes/handoffs"):
        assert config.is_transient_dir(spell, False) is True, spell
    assert config.is_transient_dir("/home/me/proj/src", False) is False
    # POSIX is case-sensitive: a CACHE dir is NOT the cache marker
    assert config.is_transient_dir("/home/me/CACHE", False) is False


def test_ledger_skips_transient_cache_dir(tmp_path, monkeypatch):
    """Owner 2026-11-17: a read whose resolved boundary is a transient cache
    tree (carrying a .git marker, i.e. a 'real' boundary) must NOT mint a
    ledger row — the transient guard fires on the boundary path itself."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    cache = tmp_path / "proj" / "cache"
    (cache / ".git").mkdir(parents=True)
    f = cache / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")
    _log_unindexed_read("read_file", {"path": str(f)}, "s1")
    ledger = tmp_path / "hh" / "plugins" / "code_map" / "data" / "_unindexed_read_ledger"
    text = ledger.read_text().strip() if ledger.exists() else ""
    assert "cache" not in text, "a transient cache dir must never be offered for indexing"


def test_ledger_still_logs_real_project_with_cache_subdir(tmp_path, monkeypatch):
    """Over-suppression guard: the boundary is only tested, not its
    subtrees — a real project that merely CONTAINS a cache subdir must still
    be offered (the boundary is the project root, not the cache dir)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    proj = tmp_path / "myproj"
    (proj / ".git").mkdir(parents=True)
    (proj / "cache").mkdir()          # a cache SUBDIR inside the project
    f = proj / "main.py"
    f.write_text("def f(): pass\n", encoding="utf-8")
    _log_unindexed_read("read_file", {"path": str(f)}, "s2")
    ledger = tmp_path / "hh" / "plugins" / "code_map" / "data" / "_unindexed_read_ledger"
    text = ledger.read_text() if ledger.exists() else ""
    assert str(proj) in text, \
        "a real project with a cache subdir must still be offered (boundary = the project)"


# ── 9. container-root never-propose guard (subdir-safe, owner 2026-11-17) ──
def test_is_container_root_blocks_bare_home_and_profiles(tmp_path, monkeypatch):
    """is_container_root: the BARE HERMES_HOME and HERMES_HOME/profiles are
    container roots (never proposed as an index target). Derived from the
    live HERMES_HOME, not hardcoded — so it tracks host/profile."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    from code_map import config
    assert config.is_container_root(str(tmp_path / "hh"), True) is True
    assert config.is_container_root(str(tmp_path / "hh" / "profiles"), True) is True


def test_is_container_root_is_subdir_safe(tmp_path, monkeypatch):
    """A sub-project UNDER the container is NOT a container root (the owner's
    'plugins 下个别文件夹也许需要索引' ruling) — it must stay indexable. Only
    the bare root matches, never a subfolder."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    from code_map import config
    assert config.is_container_root(str(tmp_path / "hh" / "plugins" / "filemem"), True) is False
    assert config.is_container_root(str(tmp_path / "hh" / "profiles" / "coder"), True) is False
    assert config.is_container_root(str(tmp_path / "myproj"), True) is False


def test_ledger_skips_bare_hermes_home_container(tmp_path, monkeypatch):
    """Write side: a read whose boundary resolves to the bare HERMES_HOME
    container must not mint a ledger row (subdir-safe: a sub-project under
    the home still logs, as test_ledger_still_logs_real_project shows)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    f = tmp_path / "hh" / "topfile.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x = 1\n", encoding="utf-8")
    _log_unindexed_read("read_file", {"path": str(f)}, "s1")
    ledger = tmp_path / "hh" / "plugins" / "code_map" / "data" / "_unindexed_read_ledger"
    text = ledger.read_text().strip() if ledger.exists() else ""
    assert text == "", f"the bare HERMES_HOME container must not mint a row: {text!r}"


# ── 10. skill-dir "不再问" name-marker (same class as hermes-file-memory-plugin) ──
def test_is_transient_dir_blocks_hermes_code_map_skill_dir(tmp_path):
    """'不再问' for the hermes-code-map SKILL dir (owner 2026-11-17 clarify):
    its leaf name is a transient marker -> a boundary under it is never
    proposed for indexing (it is a markdown skill doc, not a source project)."""
    from code_map import config
    p = str(tmp_path / "hh" / "profiles" / "coder" / "skills" / "hermes-code-map")
    assert config.is_transient_dir(p, True) is True


def test_is_transient_dir_does_not_block_code_map_plugin(tmp_path):
    """Over-suppression guard: the actual code_map PLUGIN dir (leaf 'code_map',
    underscore) is a DIFFERENT name from the skill-dir marker ('hermes-code-map',
    hyphen) -> the plugin must stay indexable / confirmable."""
    from code_map import config
    p = str(tmp_path / "hh" / "plugins" / "code_map")
    assert config.is_transient_dir(p, True) is False


# ── 11. hermes_home() pure base accessor (F1/F2 refactor, owner 2026-11-17) ──
def test_hermes_home_is_pure_no_mkdir(tmp_path, monkeypatch):
    """F1: hermes_home() returns the live base WITHOUT creating it (the old
    data_dir().parent^3 path mkdir'd; the pure accessor must not)."""
    from code_map import config
    target = tmp_path / "no_such_home"
    assert not target.exists()
    monkeypatch.setenv("HERMES_HOME", str(target))
    assert config.hermes_home() == target
    assert not target.exists(), "hermes_home() must not mkdir (F1 purity)"


def test_data_dir_still_rooted_under_hermes_home(tmp_path, monkeypatch):
    """Behavior-preserved: data_dir() == hermes_home()/plugins/code_map/data,
    and the mkdir side-effect now lives ONLY in data_dir (not in the
    hermes_home / is_container_root predicates)."""
    from code_map import config
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    home = config.hermes_home()
    assert home == tmp_path / "hh"
    assert config.data_dir() == home / "plugins" / "code_map" / "data"
    assert config.data_dir().exists(), "data_dir() still mkdirs"


def test_is_container_root_no_longer_mkdirs(tmp_path, monkeypatch):
    """F1 end-to-end: calling is_container_root against a NON-EXISTENT
    HERMES_HOME must not create the tree (the old _hermes_home_base ->
    data_dir().mkdir path is gone); the predicate stays pure."""
    from code_map import config
    target = tmp_path / "no_such_home2"
    monkeypatch.setenv("HERMES_HOME", str(target))
    assert not target.exists()
    assert config.is_container_root(str(target), True) is True
    assert not target.exists(), "is_container_root must not mkdir (F1)"


# ── 12. F5: residual container roots (AppData\Local bare + .hermes mirror) ──
def test_is_container_root_blocks_localappdata_bare(tmp_path, monkeypatch):
    """F5: the OS local-app-data BARE root (LOCALAPPDATA) is a container root
    (never proposed), derived from the live env (not hardcoded). Subdir-safe:
    a sub-project UNDER it (e.g. .../AppData/Local/hermes/plugins/filemem)
    stays open."""
    from code_map import config
    la = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(la))
    monkeypatch.setenv("HERMES_HOME", str(la / "hermes"))
    assert config.is_container_root(str(la), True) is True
    assert config.is_container_root(str(la / "hermes" / "plugins" / "filemem"), True) is False


def test_is_container_root_blocks_home_dot_hermes_mirror(tmp_path, monkeypatch):
    """F5: the home-dir .hermes relay/phantom mirror is a container root
    (never proposed), derived from Path.home() (not hardcoded). Subdir-safe:
    a plugin UNDER the mirror stays open."""
    from code_map import config
    home = tmp_path / "userhome"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    mirror = home / ".hermes"
    assert config.is_container_root(str(mirror), True) is True
    assert config.is_container_root(str(mirror / "plugins" / "filemem"), True) is False


# ── 13. residual gaps: shared home (container root) + skills class dir (owner 2026-11-17) ──
def test_is_container_root_blocks_shared_hermes_home(tmp_path, monkeypatch):
    """The SHARED hermes home (LOCALAPPDATA/hermes) is a container root
    (never proposed), distinct from the profile home that hermes_home()
    returns on this host. Subdir-safe: a plugin UNDER the shared home stays
    open; the profile home (hermes_home()) is still blocked too."""
    from code_map import config
    la = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(la))
    monkeypatch.setenv("HERMES_HOME", str(la / "hermes" / "profiles" / "coder"))
    assert config.is_container_root(str(la / "hermes"), True) is True
    assert config.is_container_root(str(la / "hermes" / "plugins" / "filemem"), True) is False
    assert config.is_container_root(str(la / "hermes" / "profiles" / "coder"), True) is True


def test_is_container_root_blocks_shared_profiles_root(tmp_path, monkeypatch):
    """F-A (owner 2026-11-17): the SHARED profiles root (LOCALAPPDATA/hermes/
    profiles, parent of every profile home) is a container root that slipped the
    exact-match set — hermes_home() returns the PROFILE home, so base/profiles
    was a phantom and the shared .../hermes/profiles was not a candidate. Now
    blocked. Subdir-safe: an unrelated profile-home / project UNDER the shared
    profiles root stays open."""
    from code_map import config
    la = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(la))
    monkeypatch.setenv("HERMES_HOME", str(la / "hermes" / "profiles" / "coder"))
    assert config.is_container_root(str(la / "hermes" / "profiles"), True) is True
    assert config.is_container_root(str(la / "hermes" / "profiles" / "other"), True) is False


def test_is_transient_dir_blocks_autonomous_ai_agents_class(tmp_path):
    """The skills CLASS dir 'autonomous-ai-agents' (markdown skill docs' parent,
    not a source project) is a transient marker -> never proposed. Over-
    suppression guard: a plain source project (no such component) is NOT blocked."""
    from code_map import config
    p = str(tmp_path / "hh" / "profiles" / "coder" / "skills" / "autonomous-ai-agents")
    assert config.is_transient_dir(p, True) is True
    assert config.is_transient_dir(str(tmp_path / "myproj"), True) is False


# ── 14. F3/F4 hardening of is_container_root (owner 2026-11-17 '可以') ──
def test_is_container_root_survives_home_resolution_failure(tmp_path, monkeypatch):
    """F3: if Path.home() raises (no home defined) the predicate must NOT crash —
    it degrades to skipping the .hermes mirror candidate. The LOCALAPPDATA
    candidate still blocks; the call returns normally (no exception)."""
    import pathlib
    import code_map.config as config
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))

    def _boom(cls):
        raise RuntimeError("no home defined")

    monkeypatch.setattr(pathlib.Path, "home", classmethod(_boom))
    # does not raise; the LOCALAPPDATA bare candidate still blocks:
    assert config.is_container_root(str(tmp_path / "la"), True) is True
    # a non-candidate still returns False (the predicate ran to completion):
    assert config.is_container_root(str(tmp_path / "la" / "somewhere"), True) is False


def test_is_container_root_matches_trailing_separator_target(tmp_path, monkeypatch):
    """F4: a hand-crafted target carrying a trailing separator
    ('...\\\\la\\\\') still matches after normalization (rstrip of \\ and /)."""
    import code_map.config as config
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    la = tmp_path / "la"
    monkeypatch.setenv("LOCALAPPDATA", str(la))
    assert config.is_container_root(str(la) + "\\", True) is True
    assert config.is_container_root(str(la / "hermes") + "/", True) is True
