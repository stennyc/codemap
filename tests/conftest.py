"""Test-suite isolation for code_map.

CODE_MAP_AUTO_INSTALL defaults to "on" in the plugin bootstrap
(__init__._maybe_auto_install_grammars — the owner directive that the
dep venv self-heals its grammar set). Inside a suite run that would be
a real side effect on the managed venv, so the suite pins it OFF unless
a test explicitly sets it (monkeypatch.setenv per-test wins over this
setdefault, because it runs later).
"""
import os

os.environ.setdefault("CODE_MAP_AUTO_INSTALL", "off")
