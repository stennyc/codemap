"""scanner.py — async build scanner: parse + upsert files in batches.

Contract ①: streaming, O(single-file) peak memory.
Contract ⑦: one WAL commit every BUILD_COMMIT_BATCH files.
Fail-soft: parse errors on individual files never abort the scan.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import List, Set

from .config import BUILD_COMMIT_BATCH, INDEXED_EXTENSIONS
from . import config
from .db import (init_db, insert_symbols, upsert_file, remove_paths,
                prune_orphan_vocab, gen_stale_paths)
from .fingerprint import file_fingerprint, invalidate_cache, on_disk_files
from .parser import parse_file, available, available_for
from .registry import EXT_TO_LANG, available_languages
from typing import FrozenSet


class BuildScanner:
    """Walks `root`, parses changed files, upserts into the DB."""

    def __init__(self, root: str, build_id: str):
        self.root = os.path.abspath(root)
        self.build_id = build_id
        self._stats = {"added": 0, "removed": 0, "parsed": 0, "skipped": 0,
                       "gen_reparsed": 0}

    def run(self) -> dict:
        """Execute the build. Returns a stats dict. Non-blocking (call in thread)."""
        conn = init_db(self.root)
        try:
            on_disk = on_disk_files(self.root)
            self._diff_and_upsert(conn, on_disk)
            invalidate_cache(self.root)
        finally:
            conn.close()
        return self._stats

    def _diff_and_upsert(self, conn, on_disk: "FrozenSet[str]") -> None:
        from .fingerprint import diff_indexed
        added, removed = diff_indexed(self.root, on_disk)

        if removed:
            remove_paths(conn, removed, commit=False, prune_vocab=False)
            self._stats["removed"] = len(removed)
        did_remove = bool(removed)

        lang_set = set(available_languages())
        # L2.3c: files indexed under an OLDER parser generation must be
        # re-parsed even when their SOURCE is unchanged — a parser/grammar
        # upgrade silently no-ops on mtime equality and the stale-generation
        # symbol rows stay visible (the L2.7 phantom-row incident: 55
        # older-generation kwarg rows in an 18k-line file, reported
        # "fresh" by staleness). '' = pre-L2.3c legacy row (unknown
        # generation) — re-parsed by design (one-shot self-heal). The
        # re-parse clears the file's rows FIRST (an older generation's
        # orphan rows are not overwritten by INSERT OR REPLACE).
        gen_stale = gen_stale_paths(conn, self.root) & on_disk
        batch: List[tuple] = []
        for fpath in sorted(added | gen_stale):
            ext = os.path.splitext(fpath)[1].lower()
            lang = EXT_TO_LANG.get(ext, "unknown")
            if lang not in lang_set:
                self._stats["skipped"] += 1
                continue
            if fpath in gen_stale:
                # L2.8j: no per-call vocab prune here — amortized to ONE
                # index-driven set op after the loop (the old correlated
                # prune per gen-re-parsed file was ~15 s/file at the live
                # 32k-row scale; 136 files ≈ 34 min).
                remove_paths(conn, {fpath}, commit=False, prune_vocab=False)
                did_remove = True
                self._stats["gen_reparsed"] += 1
            rows = parse_file(fpath)
            mtime, size = file_fingerprint(Path(fpath))
            upsert_file(conn, fpath, mtime, size, lang,
                        self.build_id, commit=False,
                        parser_gen=config.PARSER_GENERATION)
            insert_symbols(conn, self.root, fpath, rows, commit=False)
            self._stats["parsed"] += 1
            if len(batch) >= BUILD_COMMIT_BATCH:
                conn.commit()
                batch = []
        if did_remove:
            prune_orphan_vocab(conn)
        conn.commit()
        self._stats["added"] = len(added)


def scan_root(root: str, build_id: str) -> dict:
    """Convenience wrapper: run a BuildScanner for `root`."""
    return BuildScanner(root, build_id).run()
