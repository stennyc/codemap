"""Parameter-name symbol indexing (L2.1 addition, 2026-11-16).

Parameter names become kind='parameter' rows so "where is parameter X
declared / what is its default value" is answerable by NAME lookup
(code_map_query name=...) instead of a whole-tree grep. The T3 benchmark
(2026-11-16) showed code_map_query(name='min_score') returned 0 rows
while grep found 113 consumer lines — this is the gap the owner asked
to close (对标 code graph: 参数被谁消费).

v1 scope (python only):
  - emits one row per declared parameter of a (async) function definition
  - line = the parameter's OWN line (multi-line defs: the feature point)
  - sig = the parameter declaration text (name + annotation + default)
  - self/cls are NEVER indexed (universal, zero signal)
  - lambda parameters are NOT captured (v1, documented)
  - other languages: unchanged (v1)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from code_map.parser import parse_bytes  # noqa: E402

# exact line numbers matter (the feature point: each param on its own line)
SRC = b'''
class P:
    def m(self, cls_name: str = "a", b: int = 3, *args, **kwargs, flag=False) -> int:
        return 1

def plain(x, limit: int = 5, min_score: float = 0.3,
          ranking: str = "decay", include_fixture: bool = False) -> list:
    return []

async def afn(q: str, *, timeout: int = 30) -> str:
    return q
'''
#  L2 class P:
#  L3 def m(self, cls_name: str = "a", b: int = 3, *args, **kwargs, flag=False) -> int:
#  L6 def plain(x, limit: int = 5, min_score: float = 0.3,
#  L7           ranking: str = "decay", include_fixture: bool = False) -> list:
#  L10 async def afn(q: str, *, timeout: int = 30) -> str:


def _rows():
    return parse_bytes(SRC)


def test_parameter_rows_emitted():
    pnames = {n for (n, k, l, s) in _rows() if k == "parameter"}
    assert {"cls_name", "b", "args", "kwargs", "flag",
            "x", "limit", "min_score", "ranking", "include_fixture",
            "q", "timeout"} <= pnames, pnames


def test_self_cls_never_indexed():
    pnames = {n for (n, k, l, s) in _rows() if k == "parameter"}
    assert "self" not in pnames
    assert "cls" not in pnames


def test_parameter_line_is_its_own_line():
    p = {(n, l): s.strip() for (n, k, l, s) in _rows() if k == "parameter"}
    # multi-line def: min_score sits on L6 with the def, ranking drops to L7
    assert ("min_score", 6) in p
    assert p[("min_score", 6)] == "min_score: float = 0.3"
    assert ("ranking", 7) in p
    assert p[("ranking", 7)].startswith("ranking: str")
    # keyword-only params
    assert ("timeout", 10) in p
    assert p[("timeout", 10)] == "timeout: int = 30"
    # *args / **kwargs land as bare names
    assert p[("args", 3)] == "*args"
    assert p[("kwargs", 3)] == "**kwargs"


def test_existing_kinds_unaffected():
    kinds = {k for (n, k, l, s) in _rows()}
    assert {"function", "class", "method"} <= kinds, kinds
    fns = {n for (n, k, l, s) in _rows() if k in ("function", "method")}
    assert {"plain", "afn", "m"} <= fns
