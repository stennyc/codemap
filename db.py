"""SQLite per-root index store.

WAL + busy_timeout=5000ms + per-file single-writer lock (contract ⑦).
The build path batches commits every BUILD_COMMIT_BATCH files; the
hook/refresh path commits per file (gate ⑦ semantics preserved).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from .config import root_db_path, WAL_BUSY_TIMEOUT_MS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path          TEXT PRIMARY KEY,
    mtime         REAL NOT NULL,
    size          INTEGER NOT NULL,
    lang          TEXT NOT NULL,
    build_id      TEXT NOT NULL,
    fingerprint_epoch INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS symbols (
    name  TEXT NOT NULL,
    kind  TEXT NOT NULL,
    line  INTEGER NOT NULL,
    sig   TEXT NOT NULL DEFAULT '',
    path  TEXT NOT NULL,
    root  TEXT NOT NULL,
    PRIMARY KEY (root, path, name, line)
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=WAL_BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=%d" % WAL_BUSY_TIMEOUT_MS)
    return conn


def init_db(root: str) -> sqlite3.Connection:
    db = root_db_path(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def upsert_file(conn: sqlite3.Connection, path: str, mtime: float,
                size: int, lang: str, build_id: str,
                commit: bool = True) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO files(path, mtime, size, lang, build_id, fingerprint_epoch)"
        " VALUES(?, ?, ?, ?, ?, 0)",
        (path, mtime, size, lang, build_id),
    )
    if commit:
        conn.commit()


def insert_symbols(conn: sqlite3.Connection, root: str, path: str,
                   rows: list, commit: bool = True) -> None:
    for name, kind, line, sig in rows:
        conn.execute(
            "INSERT OR REPLACE INTO symbols(name, kind, line, sig, path, root)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (name, kind, line, sig, path, root),
        )
    if commit:
        conn.commit()


def query_symbols(conn: sqlite3.Connection, root: str,
                  name: Optional[str] = None,
                  path: Optional[str] = None,
                  kind: Optional[str] = None,
                  limit: int = 200,
                  offset: int = 0) -> list:
    """Query symbol rows for `root`. `path` must be an ABSOLUTE file path —
    it matches EXACTLY one indexed file (the symbols table stores absolute
    paths, see insert_symbols / _reindex_single_file_sync). A relative or
    suffix-style path used to under-match or over-match sibling files with
    the same basename (the b-1 attach contamination: `__init__.py` pulled
    in every subpackage's __init__ symbols); exact match removes both.
    """
    query = "SELECT root, path, name, kind, line, sig FROM symbols WHERE root = ?"
    params: list = [root]
    if name:
        query += " AND name = ?"
        params.append(name)
    if path:
        query += " AND path = ?"
        params.append(os.path.abspath(path))
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    query += " ORDER BY line LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return conn.execute(query, params).fetchall()


def stale_paths(conn: sqlite3.Connection, root: str,
                on_disk) -> set:
    """Return paths in the index that are no longer on disk."""
    rows = conn.execute(
        "SELECT path FROM files WHERE path LIKE ?",
        (f"{root}/%",),
    ).fetchall()
    indexed = {r[0] for r in rows}
    return indexed - set(on_disk)


def remove_paths(conn: sqlite3.Connection, paths: set,
                 commit: bool = True) -> None:
    for p in paths:
        conn.execute("DELETE FROM symbols WHERE path = ?", (p,))
        conn.execute("DELETE FROM files WHERE path = ?", (p,))
    if commit:
        conn.commit()
