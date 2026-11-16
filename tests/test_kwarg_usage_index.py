"""Keyword-argument usage indexing (L2.2 addition, 2026-11-16).

kind='kwarg_usage' rows: every keyword argument passed to a Python call,
captured no matter WHERE the call is nested (bare statement, assignment
value, comprehension, literal) — closing the caller-enumeration gap the
T3 benchmark showed ("who passes min_score=" was grep-only while
call_site rows missed nested calls).

v1 scope (python only):
  - one row per keyword_argument: name = the kwarg identifier,
    line = the kwarg's own line, sig = the `name=value` text
  - positional arguments carry no name: NOT captured (resolving which
    parameter a positional lands on needs type resolution — LSP class,
    out of the L1.5/L2 name-level contract; disclosed in the payload)
  - other languages: unchanged
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from code_map.parser import parse_bytes  # noqa: E402

#  L1 (blank)
#  L2 def target(a: int, b: float = 0.3) -> int:
#  L3     return a
#  L4 (blank)
#  L5 def caller():
#  L6     target(1, b=0.5)                    bare-statement call
#  L7     x = target(2, a=9, b=0.1)           assignment-nested call
#  L8     for _ in [target(3, b=0.2)]:        comprehension-nested call
#  L9         pass
SRC = b'''
def target(a: int, b: float = 0.3) -> int:
    return a

def caller():
    target(1, b=0.5)
    x = target(2, a=9, b=0.1)
    for _ in [target(3, b=0.2)]:
        pass
'''


def _kwarg_rows():
    return [(n, l, s) for (n, k, l, s) in parse_bytes(SRC) if k == "kwarg_usage"]


def test_kwarg_rows_captured_at_every_nesting():
    rows = {n: [(l, s) for (nm, l, s) in _kwarg_rows() if nm == n]
            for n in ("a", "b")}
    # bare statement (L6), assignment value (L7), comprehension (L8)
    assert rows["b"] == [(6, "b=0.5"), (7, "b=0.1"), (8, "b=0.2")], rows
    assert rows["a"] == [(7, "a=9")], rows


def test_positional_args_not_captured():
    # the positional `1`/`2`/`3` have no name -> no rows for them
    names = {n for (n, l, s) in _kwarg_rows()}
    assert "a" in names and "b" in names
    assert names == {"a", "b"}  # exactly the keyword args, nothing else


def test_call_site_rows_still_present():
    kinds = {k for (n, k, l, s) in parse_bytes(SRC)}
    assert "call_site" in kinds
    assert "kwarg_usage" in kinds
    # call_site remains the CONSERVATIVE bare-statement capture (L6 only —
    # the assignment/comprehension-nested calls at L7/L8 are the known
    # documented gap; kwarg_usage above is what covers their usage side).
    calls = [(n, l) for (n, k, l, s) in parse_bytes(SRC)
             if k == "call_site" and n == "target"]
    assert calls == [("target", 6)], calls
