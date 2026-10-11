"""L2 call_site edge tests: one fixture per language, gates ①②④ per language.

Run from /root/.hermes/plugins/ (shared conftest/pytest root), per standing rule:
    cd /root/.hermes/plugins && <py3.14> -m pytest code_map/tests/ -q
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Make code_map importable regardless of pytest's cwd.
import code_map.tests  # noqa: F401  (sys.path bootstrap, see tests/__init__.py)

import pytest

from code_map.parser import parse_bytes, available_for
from code_map.tests import fixtures as fx

# (lang, src, expected call_site names) — per-language fixture + expected set.
CASES = [
    ("python", fx.PYTHON_SRC, fx.PYTHON_EXPECTED_CALL_NAMES),
    ("typescript", fx.TYPESCRIPT_SRC, fx.TYPESCRIPT_EXPECTED_CALL_NAMES),
    ("javascript", fx.JAVASCRIPT_SRC, fx.JAVASCRIPT_EXPECTED_CALL_NAMES),
    ("go", fx.GO_SRC, fx.GO_EXPECTED_CALL_NAMES),
    ("rust", fx.RUST_SRC, fx.RUST_EXPECTED_CALL_NAMES),
    ("java", fx.JAVA_SRC, fx.JAVA_EXPECTED_CALL_NAMES),
    ("dart", fx.DART_SRC, fx.DART_EXPECTED_CALL_NAMES),
    ("c", fx.C_SRC, fx.C_EXPECTED_CALL_NAMES),
    ("cpp", fx.CPP_SRC, fx.CPP_EXPECTED_CALL_NAMES),
]


def _lang_available(lang: str) -> bool:
    return available_for(lang)


def _call_names(src: bytes, lang: str) -> set:
    rows = parse_bytes(src, lang=lang)
    return {name for (name, kind, _line, _sig) in rows if kind == "call_site"}


# ---- gate ①: streaming / O(single-file) — no whole-dir reads ----------------
def test_gate1_no_module_level_dir_reads():
    """parser.py must not walk a directory at import/parse time (O(1 file))."""
    import code_map.parser as p
    # parse_bytes is the pure entrypoint; assert it's a function with no
    # directory-walking helper leaking a root argument.
    import inspect
    sig = inspect.signature(p.parse_bytes)
    assert "root" not in sig.parameters, "parse_bytes must stay O(single file)"


# ---- gate ②: per-language fixture -------------------------------------------
@pytest.mark.parametrize("lang,src,expected", CASES)
def test_gate2_call_site_names_per_language(lang, src, expected):
    if not _lang_available(lang):
        pytest.skip(f"grammar for {lang} not installed in this environment")
    got = _call_names(src, lang)
    missing = expected - got
    assert not missing, f"{lang}: expected call_site names {sorted(expected)}, " \
                        f"got {sorted(got)} (missing {sorted(missing)})"


# ---- receiver-qualified calls (Java/C++ dual-grammar case) -------------------
def test_gate2_receiver_qualified_call_not_bare_identifier():
    """`obj.m()` / `std::puts()` must yield the CALLED name, not the receiver."""
    got_java = _call_names(fx.JAVA_SRC, "java")
    assert "println" in got_java, f"java receiver-qualified call failed: {got_java}"
    assert "out" not in got_java, "java must not emit the receiver 'out' as a name"

    got_cpp = _call_names(fx.CPP_SRC, "cpp")
    assert "puts" in got_cpp, f"cpp qualified call failed: {got_cpp}"
    assert "std" not in got_cpp, "cpp must not emit the namespace 'std' as a name"


# ---- gate ④: query_ms timing window includes call_site SQL -------------------
def test_gate4_query_ms_includes_call_site_path():
    """kind='call_site' must be a first-class, timed query path."""
    from code_map.config import QUERY_GATE_MS, root_db_path
    from code_map.db import init_db, insert_symbols, upsert_file
    from code_map.query import code_map_query

    import shutil
    root = "/tmp/cm_gate4_query_ms"
    shutil.rmtree(root, ignore_errors=True)
    _confirmed_root_written = False
    try:
        # Seed a tiny index: one .py file with a real call site.
        os.makedirs(root, exist_ok=True)
        fpath = os.path.join(root, "a.py")
        with open(fpath, "w") as f:
            f.write(fx.GATE4_PY_FILE.decode())
        from code_map.parser import parse_file
        from code_map.fingerprint import file_fingerprint
        conn = init_db(root)
        mtime, size = file_fingerprint(Path(fpath))
        upsert_file(conn, fpath, mtime, size, "python", "test", commit=True)
        insert_symbols(conn, root, fpath, parse_file(fpath), commit=True)
        conn.close()
        # 2026-10-11 root gate: the query surface only accepts CONFIRMED roots,
        # so register this /tmp root (pytest's own tmp dirs land under
        # /root/.hermes/... which is a gate-10 blocked prefix — same
        # known-pitfall as test_root_legality.py / test_check_root.py).
        from code_map import rootreg
        rootreg.write_confirmed_root(root)
        _confirmed_root_written = True

        t0 = time.perf_counter()
        out = code_map_query({"name": "helper", "kind": "call_site", "root": root})
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        conn = init_db(root)
        conn.close()

        import json
        payload = json.loads(out)
        assert payload["query_ms"] >= 0, "query_ms must be present and non-negative"
        assert payload["count"] >= 1, (
            f"kind=call_site query for 'helper' must return >=1 row, got {payload}")
        assert elapsed_ms < QUERY_GATE_MS * 5, (
            f"call_site query {elapsed_ms}ms blew the sanity ceiling "
            f"({QUERY_GATE_MS * 5}ms)")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        # Owner rule (2026-10-11): tests must not pollute the production
        # _registry with a dead confirmed target — a subsequent hook run
        # would nudge against a vanished /tmp dir. Remove only this test's
        # entry; leave all other (live) confirmed targets untouched.
        if _confirmed_root_written:
            from code_map import rootreg
            reg = rootreg._registry_file()
            if reg.exists():
                kept = [l for l in reg.read_text().splitlines()
                        if l.strip() and os.path.abspath(l.strip()) != root]
                reg.write_text(
                    "\n".join(kept) + ("\n" if kept else ""),
                    encoding="utf-8")


# ---- no false rows: bare expression statement without call args ---------------
def test_gate2_no_call_site_row_for_non_call_expression_statement():
    """A dart expression statement that is NOT a call (no args) yields no row."""
    from code_map.parser import parse_bytes as pb
    src = b"void main() {\n    x = 1;\n}\n"
    rows = pb(src, lang="dart")
    assert all(kind != "call_site" for (_n, kind, _l, _s) in rows), \
        "assignment statement must not be misread as a call_site"


if __name__ == "__main__":
    import sys as _s
    _s.exit(pytest.main([__file__, "-v"]))
