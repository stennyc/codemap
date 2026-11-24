"""§2.21 (2026-11-21 owner ruling, synced from the DSH port): the
system-path three-tier split —

  1. hard-block core (never indexable, even explicitly): bare "/" +
     the device/pseudo trio /dev /proc /sys;
  2. silent prefixes (pitch-silent, confirm gate OPEN): /bin /sbin
     /etc /usr /opt /var;
  3. dot rule: a dot-named project dir with no ROOT_MARKERS member
     inside is pitch-silent; a hidden repo stays askable.

The Windows twin of the split is deferred by owner ruling ("Windows 侧
留到在 windows 系统才做") — on Windows is_silent_tree is always False
and is_system_tree keeps the C:\\Windows family hard-block.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # 自适应 bootstrap: plugin 根

from code_map import config


def test_silent_prefixes_posix():
    for p in ("/bin", "/sbin", "/etc", "/usr", "/usr/local/bin", "/opt", "/var", "/var/www/proj"):
        assert config.is_silent_tree(p, False), f"{p} must be a silent prefix"
    for p in ("/root/proj", "/home/me/proj", "/mnt/data/proj", "/srv/www", "/media/usb",
              "/dev/x", "/proc/x", "/sys/x"):
        assert not config.is_silent_tree(p, False), f"{p} must NOT be a silent prefix"


def test_silent_prefixes_windows_deferred():
    # owner ruling 2026-11-21: the Windows silent list is deferred —
    # Windows is never silent-tree on this pass.
    for p in ("C:\\Program Files\\x", "C:\\Users\\me\\proj", "/usr", "/etc"):
        assert config.is_silent_tree(p, True) is False, f"{p}: Windows deferred"


def test_system_tree_hard_core_posix():
    # §2.21: the legacy triple demoted — /usr /etc /opt are no longer
    # a hard block; the device/pseudo trio + bare "/" are.
    for p in ("/dev/x", "/proc", "/sys", "/", ""):
        assert config.is_system_tree(p, False), f"{p} must stay a hard-block system tree"
    for p in ("/usr/local/bin", "/etc/nginx", "/opt/dev-tools", "/root/proj", "/home/me/proj", "/mnt/data"):
        assert not config.is_system_tree(p, False), f"{p} must no longer be a hard block"


def test_dot_root_silent(tmp_path):
    venv = tmp_path / ".venv"
    venv.mkdir()
    assert config.dot_root_silent(str(venv)), "a marker-less dot dir must be dot-silent"

    repo = tmp_path / ".repo"
    (repo / ".git").mkdir(parents=True)
    assert not config.dot_root_silent(str(repo)), "a dot dir with .git inside must stay askable"

    plain = tmp_path / "proj"
    plain.mkdir()
    assert not config.dot_root_silent(str(plain)), "a non-dot dir must never be dot-silent"
