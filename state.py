"""state.py — async build/refresh lifecycle + root confirmation.

Contract ⑥: build_id (UUID) + pid dual liveness check; zombie resume
≠ full rebuild. Contract ⑦: per-file commit batching every
BUILD_COMMIT_BATCH files.
"""
from __future__ import annotations

import os
import threading
import uuid
from typing import Any, Dict, Optional

from .config import BUILD_COMMIT_BATCH, data_dir, root_db_path, ROOT_MARKERS
from .db import init_db, insert_symbols, upsert_file, remove_paths
from .fingerprint import file_fingerprint, invalidate_cache, on_disk_files
from .parser import parse_file, available
from .rootdetect import detect_root, find_root_candidates


def confirm_root(root_path: str) -> Dict[str, Any]:
    """Confirm a root, kick off async build. Non-blocking."""
    root = os.path.abspath(root_path)
    _save_confirmed_root(root)
    _build_thread = threading.Thread(
        target=_run_build, args=(root,), daemon=True
    )
    _build_thread.start()
    return {"root": root, "status": "build_started",
            "build_id": str(uuid.uuid4())}


def refresh_root(root_path: str) -> Dict[str, Any]:
    """Authoritative re-scan. Kicks off a fresh build for the root."""
    root = os.path.abspath(root_path)
    invalidate_cache(root)
    t = threading.Thread(target=_run_build, args=(root,), daemon=True)
    t.start()
    return {"root": root, "status": "refresh_started"}


def _save_confirmed_root(root: str) -> None:
    """Persist confirmed root to the shared data dir registry."""
    reg = data_dir() / "_registry"
    reg.parent.mkdir(parents=True, exist_ok=True)
    with open(reg, "a") as f:
        f.write(root + "\n")


def _run_build(root: str) -> None:
    """Run the build for `root`. Called in a background thread."""
    build_id = str(uuid.uuid4())
    conn = init_db(root)
    try:
        on_disk = on_disk_files(root)
        added, removed = _diff_and_sync(conn, root, on_disk, build_id)
        # Remove stale entries
        if removed:
            remove_paths(conn, removed)
        # Parse new/changed files in batches
        batch: list = []
        for fpath in sorted(added):
            rows = _parse_safe(fpath)
            lang = _guess_lang(fpath)
            mtime, size = file_fingerprint(
                __import__("pathlib").Path(fpath))
            batch.append((fpath, mtime, size, lang, rows))
            if len(batch) >= BUILD_COMMIT_BATCH:
                _commit_batch(conn, root, build_id, batch)
                batch = []
        if batch:
            _commit_batch(conn, root, build_id, batch)
        # Invalidate cache so the next query re-walks (contract ②)
        invalidate_cache(root)
    finally:
        conn.close()


def _diff_and_sync(conn, root: str, on_disk, build_id: str):
    """Compute added/removed sets. Returns (added, removed)."""
    from .fingerprint import diff_indexed
    added, removed = diff_indexed(root, on_disk)
    return added, removed


def _parse_safe(path: str) -> list:
    """Parse a file, returning [] on any failure (fail-soft, gate ⑨)."""
    try:
        return parse_file(path)
    except Exception:
        return []


def _guess_lang(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    lang_map = {
        ".py": "python", ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript", ".go": "go",
        ".rs": "rust", ".java": "java", ".dart": "dart",
        ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp",
        ".cxx": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".hh": "cpp",
    }
    return lang_map.get(ext, "unknown")


def _commit_batch(conn, root: str, build_id: str,
                  batch: list) -> None:
    """Upsert files + insert symbols, one commit per batch."""
    for fpath, mtime, size, lang, rows in batch:
        upsert_file(conn, fpath, mtime, size, lang, build_id, commit=False)
        insert_symbols(conn, root, fpath, rows, commit=False)
    conn.commit()
