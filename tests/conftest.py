"""Test-suite isolation for code_map.

CODE_MAP_AUTO_INSTALL defaults to "on" in the plugin bootstrap
(__init__._maybe_auto_install_grammars — the owner directive that the
dep venv self-heals its grammar set). Inside a suite run that would be
a real side effect on the managed venv, so the suite pins it OFF unless
a test explicitly sets it (monkeypatch.setenv per-test wins over this
setdefault, because it runs later).

Additionally: on this deploy TMPDIR points inside the .hermes profile
tree (.../profiles/coder/cache/scratch), so pytest's DEFAULT basetemp
(tempfile.gettempdir()/pytest-of-<user>) lands under a path containing
"scratch"/"cache" components, and every fixture built from tmp_path is
then vetoed by the transient-dir / container-root guards (owner
2026-11-17 "临时目录永不再问") — the fixtures behave like real transient
caches, not fake source projects. Upstream (TMPDIR not under .hermes)
never hits this, which is why CI is green while a host-local run shows
false failures. The module-level setdefault below pins TMPDIR off the
.hermes tree for the whole pytest process (tmp_path_factory derives
from it lazily, at first fixture use); a test's own monkeypatch.setenv
still wins per-test.
"""
import os

os.environ.setdefault("CODE_MAP_AUTO_INSTALL", "off")

# ── basetemp isolation: TMPDIR must not sit under a .hermes tree ────────
# Default $TMPDIR on this host points at the profile's cache/scratch dir;
# redirecting it here (before any tmp fixture is instantiated) puts the
# per-test tmp dirs in a neutral system temp root so the
# is_transient_dir / is_container_root guards see real project shapes,
# not cache shapes. Fail-soft: on any hiccup we keep the process as-is
# (tests may then fail on this deploy, exactly as before this guard —
# never worse than the status quo).
try:
    _tmp = os.environ.get("TMPDIR") or ""
    if "hermes" in _tmp.lower():
        # A neutral temp root that is NOT under the .hermes tree, and NOT
        # itself a transient marker (cache/scratch/etc.): /tmp on POSIX,
        # %TEMP% on Windows (a real deploy's user temp is never under a
        # .hermes tree). PYTEST_DEBUG_TEMPROOT is read by pytest's
        # TempPathFactory at first fixture use (after conftest load), so
        # setting it here re-roots every tmp_path in the suite.
        _neutral = "/tmp"
        if os.name == "nt":
            _neutral = os.environ.get("TEMP") or os.environ.get("TMP") or "C:\\"
        if "hermes" not in _neutral.lower():
            os.environ["TMPDIR"] = _neutral
            os.environ["PYTEST_DEBUG_TEMPROOT"] = _neutral
except Exception:
    pass
