"""rootreg.py — confirmed-root registry: the ONLY valid-root source.

Owner's rule (2026-10-11, v2): the gate is a CONCATENATION rule, not a
blacklist. confirm takes `root` (a boundary prefix, e.g. /root or
/usr/local) + `path` (a relative sub-path inside it); the effective
index target is normpath(join(root, path)). Gate 10 passes iff BOTH
fields are present and the joined target is not itself a bare system
root ("/"). A bare boundary with an empty path (index the whole
/home — the 382k-file incident) is refused; an empty root with an
absolute path (no boundary) is refused.

The registry stores the EFFECTIVE TARGET (the joined full path). A
query `root` is valid iff it equals a stored target.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from .config import data_dir


def _registry_file():
    return data_dir() / "_registry"


def _dirty_file():
    """Dirty-flag store: one line per confirmed target whose index lags the
    source after in-place edits (件 3 consistency). Separate from _registry so
    a dirty stamp never affects the confirm/refresh gate — it is pure signal
    for the read-side auto-refresh. Fail-soft everywhere (corrupt/absent ->
    treated as 'not dirty', which just means 'no auto-refresh needed')."""
    return data_dir() / "_dirty"


def _heal_attempts_file():
    """Per-target count of CONSECUTIVE failed read-side heal attempts
    (reviewer F2 give-up guard). Format: `target<TAB>count` one per line.
    Kept separate from _dirty so clearing the dirty stamp (size/give-up/
    success paths) does not wipe the attempt counter, and vice versa.
    Fail-soft: absent/corrupt -> 0 attempts (i.e. 'still may retry')."""
    return data_dir() / "_heal_attempts"


def _read_heal_attempts() -> Dict[str, int]:
    f = _heal_attempts_file()
    if not f.exists():
        return {}
    out: Dict[str, int] = {}
    try:
        for line in f.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                out[os.path.abspath(parts[0].strip())] = int(parts[1])
    except (OSError, ValueError):
        return {}
    return out


def _write_heal_attempts(table: Dict[str, int]) -> None:
    f = _heal_attempts_file()
    body = "".join(f"{k}\t{v}\n" for k, v in sorted(table.items()))
    try:
        f.write_text(body, encoding="utf-8")
    except OSError:
        pass  # fail-soft: a counter hiccup must never break a hook


def heal_attempts(target: str) -> int:
    """Consecutive failed-heal count for `target` (0 if never / cleared)."""
    target = os.path.abspath(target)
    return _read_heal_attempts().get(target, 0)


def note_heal_failure(target: str) -> None:
    """Bump `target`'s consecutive-failed-heal counter."""
    target = os.path.abspath(target)
    table = _read_heal_attempts()
    table[target] = table.get(target, 0) + 1
    _write_heal_attempts(table)


def reset_heal_attempts(target: str) -> None:
    """Clear `target`'s counter (after a successful heal)."""
    target = os.path.abspath(target)
    table = _read_heal_attempts()
    if target in table:
        del table[target]
        _write_heal_attempts(table)


def mark_dirty(target: str) -> None:
    """Record that `target`'s index may lag the source (after a hook reindex)."""
    target = os.path.abspath(target)
    f = _dirty_file()
    current: List[str] = []
    if f.exists():
        try:
            current = [l for l in f.read_text().splitlines() if l.strip()]
        except OSError:
            current = []
    if target not in current:
        current.append(target)
    try:
        f.write_text("\n".join(current) + ("\n" if current else ""), encoding="utf-8")
    except OSError:
        pass  # fail-soft: a dirty-mark write hiccup must never break a hook


def clear_dirty(target: str) -> None:
    """Clear `target`'s dirty stamp (after a fresh rebuild catches up)."""
    target = os.path.abspath(target)
    f = _dirty_file()
    if not f.exists():
        return
    try:
        remaining = [l for l in f.read_text().splitlines()
                     if l.strip() and os.path.abspath(l.strip()) != target]
        f.write_text("\n".join(remaining) + ("\n" if remaining else ""),
                     encoding="utf-8")
    except OSError:
        pass


def is_dirty(target: str) -> bool:
    """True iff `target` is stamped dirty (index may lag the source)."""
    target = os.path.abspath(target)
    f = _dirty_file()
    if not f.exists():
        return False
    try:
        lines = [l for l in f.read_text().splitlines() if l.strip()]
    except OSError:
        return False
    return any(os.path.abspath(l.strip()) == target for l in lines)


def _join_target(root: str, path: str) -> Optional[str]:
    """Validate the (root, path) pair and return the effective target.

    Returns None when the pair fails gate 10:
      - root empty, or path empty          (both fields mandatory)
      - root normalizes to "/"             (bare filesystem root)
      - path is absolute                   (no boundary semantics)
      - joined target escapes root        (`../` traversal)
      - joined target is "/" or empty
    """
    root = (root or "").strip()
    path = (path or "").strip()
    if not root or not path:
        return None
    root_abs = os.path.abspath(root)
    if root_abs == os.sep:
        return None
    if os.path.isabs(path):
        return None
    target = os.path.normpath(os.path.join(root_abs, path))
    if target in ("", os.sep):
        return None
    # The target must stay inside the boundary root (no ../ escape).
    if target != root_abs and not target.startswith(root_abs + os.sep):
        return None
    return target


def confirmed_targets() -> List[str]:
    """Sorted, de-duplicated list of confirmed index targets.

    (Replaces the v1 name confirmed_roots; the stored value is the
    joined target, not the bare boundary root.)"""
    reg = _registry_file()
    if not reg.exists():
        return []
    try:
        lines = [l.strip() for l in reg.read_text().splitlines() if l.strip()]
    except OSError:
        return []
    out: List[str] = []
    for target in lines:
        abspath = os.path.abspath(target)
        if abspath == os.sep or abspath in out:
            continue
        out.append(abspath)
    return sorted(out)


def is_confirmed_root(root: str) -> bool:
    """Exact membership test against the stored targets."""
    return os.path.abspath(root) in confirmed_targets()


def write_confirmed_root(target: str) -> None:
    """Append a validated target. Callers must have run _join_target
    (or state.confirm_root, which does) before calling this."""
    abspath = os.path.abspath(target)
    if abspath == os.sep:
        raise ValueError("bare filesystem root '/' can never be confirmed")
    reg = _registry_file()
    reg.parent.mkdir(parents=True, exist_ok=True)
    with open(reg, "a") as f:
        f.write(abspath + "\n")
