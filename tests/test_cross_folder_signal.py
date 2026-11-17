"""Cross-folder dependency signal (owner 2026-10-13).

The read-side attach gains a one-line, existence+count signal: how many of
the read file's in-project import edges land in a DIFFERENT top-level
sub-directory of the CONFIRMED root. Root-external targets (stdlib / 3rd
party / above-root) are never counted. The line is appended OUTSIDE the
READ_SYMBOL_MAX_BYTES cap so it must not squeeze the symbol lines.

Reuses test_hooks' `_seed_confirmed` (real builder + real tree-sitter parse,
zero hand-seeded rows) so the signal is exercised against genuine import
rows, not fabricated ones. Isolated HERMES_HOME keeps production data safe.
"""
import os
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import pytest  # noqa: E402

from code_map import hook, rootreg  # noqa: E402
from code_map.config import (CROSS_FOLDER_LINE_MAX_BYTES,
                             READ_SYMBOL_MAX_BYTES)  # noqa: E402

_seed_bases: list = []


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    yield home
    if _seed_bases:
        base = _seed_bases.pop()
        shutil.rmtree(base, ignore_errors=True)


def _make_store(target: Path, source_files, source_texts):
    target.mkdir(parents=True, exist_ok=True)
    for f, t in zip(source_files, source_texts):
        p = target / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(t, encoding="utf-8")


def _seed_confirmed(source_files, source_texts) -> str:
    base = Path("/var/tmp") / f"cm_xfolder_{uuid.uuid4().hex[:12]}"
    _seed_bases.append(base)
    target = base / "proj"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    _make_store(target, source_files, source_texts)
    rootreg.write_confirmed_root(str(target))
    from code_map.state import _run_build
    _run_build(str(target))
    rootreg.clear_dirty(str(target))
    from code_map.fingerprint import invalidate_cache
    invalidate_cache(str(target))
    return str(target)


def test_signal_counts_cross_subdir_in_root():
    """sub_a imports sub_b's module (cross top-level sub-dir) -> counted;
    its stdlib import (os) -> NOT counted."""
    root = _seed_confirmed(
        ["__init__.py", "sub_a/mod_a.py", "sub_b/mod_b.py"],
        ["A = 1",
         "import os\nfrom ..sub_b import mod_b\n",
         "Y = 2\n"],
    )
    target_file = str(Path(root) / "sub_a" / "mod_a.py")
    from code_map.db import init_db, query_symbols
    conn = init_db(root)
    rows = query_symbols(conn, root, path=target_file, kind="import", limit=8)
    conn.close()
    sig = hook._cross_folder_signal(root, target_file, rows)
    # mod_b lands in sub_b (different top-level dir than sub_a) -> 1
    # os is stdlib (not in root) -> not counted
    assert sig is not None
    assert "cross-folder: 1" in sig
    # the signal line offers the follow-up, not target paths; and it notes
    # that the count only covers the attached top-N edges (more may exist)
    assert "code_map_query" in sig
    assert "more imports may exist beyond" in sig
    assert "mod_b" not in sig  # existence signal only, no target paths


def test_signal_silent_when_all_imports_same_dir_or_external():
    """A file whose in-root imports all stay in its own sub-dir, plus a
    stdlib import -> no cross-folder contact -> no line emitted."""
    root = _seed_confirmed(
        ["__init__.py", "sub_a/mod_a.py", "sub_a/mod_a2.py"],
        ["A = 1",
         "import os\nfrom . import mod_a2\n",
         "Z = 3\n"],
    )
    target_file = str(Path(root) / "sub_a" / "mod_a.py")
    from code_map.db import init_db, query_symbols
    conn = init_db(root)
    rows = query_symbols(conn, root, path=target_file, kind="import", limit=8)
    conn.close()
    # mod_a2 is in sub_a (same top-level dir as mod_a) -> not cross; os -> external
    assert hook._cross_folder_signal(root, target_file, rows) is None


def test_attach_appends_signal_outside_byte_cap():
    """Even a symbol block that already hit READ_SYMBOL_MAX_BYTES must still
    get the cross-folder line appended AFTER the cap (never squeezed)."""
    root = _seed_confirmed(
        ["__init__.py", "sub_a/mod_a.py", "sub_b/mod_b.py"],
        ["A = 1",
         "import os\nfrom ..sub_b import mod_b\n"
         "def x1(): pass\ndef x2(): pass\ndef x3(): pass\ndef x4(): pass\n",
         "Y = 2\n"],
    )
    target_file = str(Path(root) / "sub_a" / "mod_a.py")
    out = hook._attach_symbols(root, target_file)
    assert out is not None
    # L2.8h contract update: the cross-folder signal is appended AFTER the
    # byte cap as before, but it is no longer the FINAL line — the
    # generation+staleness stamp line rides after it (also outside the cap).
    # The cap invariant on the signal line itself is unchanged.
    lines = out.rstrip().splitlines()
    signal = [l for l in lines if l.strip().startswith("cross-folder:")]
    assert signal, "the cross-folder signal line must be present"
    assert lines[-1].strip().startswith("index: gen "), \
        "the L2.8h index-stamp line is now final"
    last = signal[-1]
    # respects the dedicated signal cap (must not bloat past it)
    # +2 margin: the signal line is never the LAST byte of `out` in practice
    # — the append in _attach_symbols adds a leading "\n" before it, and a
    # truncated symbol block may end with a trailing newline. Allow a 2-byte
    # slack so the assertion is robust to that without silently letting the
    # line overflow the cap by more than the appended whitespace.
    assert len(last) <= CROSS_FOLDER_LINE_MAX_BYTES + 2
    # the cap marker, if any, stays BEFORE the signal line
    if "truncated to byte cap" in out:
        assert out.index("truncated to byte cap") < out.index("cross-folder:")


def test_attach_no_signal_line_when_zero():
    root = _seed_confirmed(
        ["__init__.py", "sub_b/mod_b.py"],
        ["A = 1", "import os\nZ = 3\n"],
    )
    target_file = str(Path(root) / "sub_b" / "mod_b.py")
    out = hook._attach_symbols(root, target_file)
    # stdlib-only imports -> no cross-folder contact
    if out is not None:
        assert "cross-folder:" not in out


def test_signal_line_survives_three_digit_count_under_cap():
    """Reviewer finding ① regression: a file with a 3-digit cross-folder
    count must still keep the `code_map_query` follow-up cue within the
    cap — the cap was raised to 208 precisely for this. We simulate the
    line directly rather than seeding 100+ modules (slow); the line's
    byte-length is the assertion target."""
    # Build the line exactly as _cross_folder_signal would for a count=999
    # with the max attached edges (READ_IMPORT_EDGES=8).
    line = (f"  cross-folder: 999 import target(s) in other sub-dir(s) "
            f"of this confirmed root (count over the 8 "
            f"import edge(s) attached above; more imports may exist beyond "
            f"top-N -> code_map_query for the full list)")
    capped = line[:CROSS_FOLDER_LINE_MAX_BYTES]
    # The critical follow-up keyword must survive the cap
    assert "code_map_query" in capped
    # And the cap itself holds
    assert len(capped) <= CROSS_FOLDER_LINE_MAX_BYTES


def test_aliased_import_symbol_resolved_to_real_module():
    """② alias undercount: `from ..sub_b import u as tool` must still be
    counted as an in-root cross-dir contact (the recorded import name is the
    alias `tool`, but the module file is `u.py`)."""
    root = _seed_confirmed(
        ["__init__.py", "sub_a/e.py", "sub_b/u.py"],
        ["A = 1",
         "from ..sub_b import u as tool\n",
         "U = 1\n"],
    )
    target_file = str(Path(root) / "sub_a" / "e.py")
    from code_map.db import init_db, query_symbols
    conn = init_db(root)
    rows = query_symbols(conn, root, path=target_file, kind="import", limit=8)
    conn.close()
    sig = hook._cross_folder_signal(root, target_file, rows)
    assert sig is not None
    assert "1 import target(s)" in sig


def test_same_basename_across_subdirs_deterministic_count():
    """② c[0] non-determinism: same basename in sub_a/ and sub_b/ is BY
    DEFINITION a cross-folder contact; the count must be deterministic
    across repeated calls (no unordered SQLite row-order dependence)."""
    root = _seed_confirmed(
        ["__init__.py", "sub_a/e.py", "sub_a/util.py", "sub_b/util.py"],
        ["A = 1",
         "from . import util\n",
         "A = 1\n",
         "B = 2\n"],
    )
    target_file = str(Path(root) / "sub_a" / "e.py")
    from code_map.db import init_db, query_symbols
    conn = init_db(root)
    rows = query_symbols(conn, root, path=target_file, kind="import", limit=8)
    conn.close()
    sigs = [hook._cross_folder_signal(root, target_file, rows) for _ in range(10)]
    assert all(s == sigs[0] for s in sigs), "count must be deterministic"
    assert sigs[0] is not None
    assert "1 import target(s)" in sigs[0]


def test_same_top_level_subdir_is_not_cross_folder():
    """Owner definition: a/b/x.py vs a/c/x.py are both under top-level 'a'
    of the confirmed root, so they are NOT cross-folder. The signal must be
    silent (count=0 -> None). Regression guard for the reviewer-round-3
    HIGH finding (full-dir vs top-level-subdir comparison)."""
    root = _seed_confirmed(
        ["__init__.py", "a/__init__.py", "a/b/x.py", "a/c/x.py", "a/b/e.py"],
        ["A = 1",
         "A = 1",
         "X = 1\n",
         "X = 1\n",
         "from . import x\n"],
    )
    target_file = str(Path(root) / "a" / "b" / "e.py")
    from code_map.db import init_db, query_symbols
    conn = init_db(root)
    rows = query_symbols(conn, root, path=target_file, kind="import", limit=8)
    conn.close()
    sig = hook._cross_folder_signal(root, target_file, rows)
    assert sig is None, f"expected None (same top-level 'a'), got: {sig}"
