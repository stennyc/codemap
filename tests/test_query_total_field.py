"""Query-response 'total' field: paged results report the TOTAL match
count (across all pages), not just the page's row count.

Motivation (2026-11-16 L2.2 benchmark): a byte-capped page returned
count=22 (rows on this page) for a 76-row match set; a consumer that
stopped at page 1 (reading 'count=22 <= limit=100, so no pagination
needed') silently undercounted by 3.8x. `total` makes the full-match
size visible on every page: page1{count:22, total:76, cursor:"22"}.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from code_map.db import init_db, insert_symbols, upsert_file  # noqa: E402
from code_map.parser import parse_file, available_for  # noqa: E402
from code_map.fingerprint import file_fingerprint  # noqa: E402
from code_map import rootreg  # noqa: E402
from code_map.query import code_map_query  # noqa: E402

import json  # noqa: E402


def _seed(tmp_path, source_text: str, monkeypatch) -> str:
    """One-file index with confirmed root; returns the root string.

    HERMES_HOME isolation (owner 2026-11-16, _registry leak fix): without
    this, write_confirmed_root appended pytest tmp dirs to the PRODUCTION
    _registry (the L9-L11 leak: pytest-of-user\\pytest-2\\test_* rows in
    both host-level and profile-level data dirs). Redirect data_dir into
    the per-test tmp before anything touches the registry.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    root = str(tmp_path)
    fpath = os.path.join(root, "a.py")
    with open(fpath, "w") as f:
        f.write(source_text)
    conn = init_db(root)
    mtime, size = file_fingerprint(Path(fpath))
    upsert_file(conn, fpath, mtime, size, "python", "total-test", commit=True)
    insert_symbols(conn, root, fpath, parse_file(fpath), commit=True)
    conn.close()
    rootreg.write_confirmed_root(root)
    return root


def _page(root, name, limit, cursor, kind="kwarg_usage"):
    return json.loads(code_map_query({
        "root": root, "name": name, "kind": kind,
        "limit": limit, "cursor": cursor}))


SRC5 = ("def f(x=1):\n    pass\n\n"
        + "\n".join(f"r{i} = f(x={i})" for i in range(5)))


def test_total_present_and_constant_across_pages(tmp_path, monkeypatch):
    if not available_for("python"):
        import pytest
        pytest.skip("python grammar not installed")
    root = _seed(tmp_path, SRC5, monkeypatch)
    p1 = _page(root, "x", 2, "0")
    assert p1["count"] == 2, p1
    assert p1["total"] == 5, p1
    assert p1["cursor"] == "2", p1

    p2 = _page(root, "x", 2, "2")
    assert p2["count"] == 2, p2
    assert p2["total"] == 5, p2

    p3 = _page(root, "x", 2, "4")
    assert p3["count"] == 1, p3
    assert p3["total"] == 5, p3

    p4 = _page(root, "x", 2, "5")
    assert p4["count"] == 0, p4
    assert p4["total"] == 5, p4


def test_total_is_filter_aware(tmp_path, monkeypatch):
    if not available_for("python"):
        import pytest
        pytest.skip("python grammar not installed")
    root = _seed(tmp_path, "def g(p=1, q=2):\n    pass\n\ng(q=9)\ng(p=3)\n", monkeypatch)
    # kwarg_usage rows only: p -> 1 (g(p=3)), q -> 1 (g(q=9))
    rp = _page(root, "p", 20, "0")
    rq = _page(root, "q", 20, "0")
    assert rp["total"] == 1, rp
    assert rq["total"] == 1, rq
    # kind-less: p rows = 1 parameter + 1 kwarg_usage
    rp_all = json.loads(code_map_query({
        "root": root, "name": "p", "limit": 20, "cursor": "0"}))
    assert rp_all["total"] == 2, rp_all


def test_total_on_unbuilt_index_is_zero(tmp_path, monkeypatch):
    if not available_for("python"):
        import pytest
        pytest.skip("python grammar not installed")
    root = _seed(tmp_path, "def h(a=1):\n    pass\n", monkeypatch)
    p = _page(root, "z_does_not_exist", 5, "0")
    assert p["count"] == 0, p
    assert p["total"] == 0, p
