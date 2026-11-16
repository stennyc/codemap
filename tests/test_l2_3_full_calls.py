"""L2.3 full syntactic call volume + inheritance/decorator edges (python v1).

Owner ruling (2026-11-16, roadmap ② after ① L2.5 shipped): the call_site
layer moves from "conservative direct-child capture" to EVERY
syntactically-visible call expression at any nesting depth (arguments,
assignment values, dict literals, comprehensions, ternaries, lambda
bodies — each `call` node visited exactly once); plus two new edge
kinds over existing node shapes:
  - kind='decorator': the callee of `@dec` / `@dec(...)` (last identifier
    of the decorator's function chain — `@app.route("/x")` -> "route")
  - kind='extends':   class bases — `class C(A, A.B):` -> rows A and B
    (name-level: dotted bases yield the LAST segment, disclosed)
Honest ceiling (disclosed in the query payload): dynamic dispatch
(`getattr(x, "f")()`) is syntactically visible only at the WRAPPER level
— the row names "getattr"; the string-dispatched target is never a row.
Positional-argument attribution remains grep/LSP territory (unchanged).

Grammar-version pitfall (L2.1 lesson, probed 2026-11-16 on this host):
the class-base list is an `argument_list` node in the current
tree-sitter-python build (NOT the legacy `superclass_list` wrapper) —
the match set carries both so a grammar upgrade cannot silently drop
rows. The same probe confirmed: decorators wrap a `decorator` node whose
payload is identifier | attribute | call.

Scope: python v1 (like L2.1/L2.2). Other languages keep the conservative
direct-child capture (documented in the parser docstring).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURE = """\
import os
from collections import defaultdict

class Base:
    def hook(self):
        return None

class Child(Base, A.B):
    pass

@staticmethod
def plain() -> int:
    return 1

@app.route("/x")
def decorated(x=3):
    return x

def top():
    f(g(1))
    d = {"k": f(g(2)), "j": [f(y) for y in z]}
    t = f(g(3)) if cond else f(g(4))
    fn = lambda: f(g(5))
    outer(inner(x=1))
    return getattr(x, "f")()

def other(a=1, b=2):
    return a + b
"""


def _rows(tmp_path):
    p = tmp_path / "l23.py"
    p.write_text(FIXTURE, encoding="utf-8")
    for m in [m for m in list(sys.modules) if m.startswith("code_map")]:
        sys.modules.pop(m, None)
    from code_map import parser
    return parser.parse_file(str(p))


def _by_kind(rows):
    out = {}
    for name, kind, line, sig in rows:
        out.setdefault(kind, []).append((name, line))
    return out


# ── 1. nested calls at every syntactic position (the L2.3 core) ──────────
def test_nested_calls_full_volume(tmp_path):
    rows = _rows(tmp_path)
    calls = sorted(n for n, _ in _by_kind(rows).get("call_site", []))
    # f x6: statement f(g(1)); dict value f(g(2)); comprehension f(y);
    #   ternary f(g(3)) & f(g(4)); lambda body f(g(5))
    # g x5: the nested argument of each of the 5 f(g(n)) sites
    # getattr x2: `getattr(x, "f")()` is TWO call nodes — the inner
    #   getattr(...) call and the outer (string-dispatch) call on it;
    #   both name the wrapper ("getattr"), the dispatched "f" is never
    #   a row (the disclosed dynamic-dispatch gap)
    # inner/outer x1 each: `outer(inner(x=1))` — the nested pair
    # route x1: the decorator call `@app.route("/x")` is a call expression
    expect = sorted(["f"] * 6 + ["g"] * 5 + ["getattr"] * 2 + ["inner", "outer", "route"])
    assert calls == expect, f"call_site rows {calls} != expected {expect}"


# ── 2. decorator rows: bare identifier + callee-chain of a decorator call ─
def test_decorator_rows(tmp_path):
    rows = _rows(tmp_path)
    dec = sorted(n for n, _ in _by_kind(rows).get("decorator", []))
    assert dec == ["route", "staticmethod"], dec
    # and the decorated defs themselves are still function rows:
    funcs = sorted(n for n, k in _by_kind(rows).get("function", []))
    assert "plain" in funcs and "decorated" in funcs, funcs


# ── 3. extends rows: identifier bases AND dotted bases (last segment) ────
def test_extends_rows(tmp_path):
    rows = _rows(tmp_path)
    ext = sorted(n for n, _ in _by_kind(rows).get("extends", []))
    assert ext == ["B", "Base"], ext


# ── 4. dynamic dispatch: wrapper row only, string target never a row ─────
def test_dynamic_dispatch_disclosed_gap(tmp_path):
    rows = _rows(tmp_path)
    calls = _by_kind(rows).get("call_site", [])
    lines = [ln for nm, ln in calls]
    # the getattr(x, "f")() call sits on its own line; its ONLY row is the
    # wrapper ('getattr' name) — the string-dispatched 'f' must NOT appear
    # as a call_site row on that line:
    getattr_line = [ln for nm, ln in calls if nm == "getattr"][0]
    assert all(nm != "f" for nm, ln in calls if ln == getattr_line)


# ── 5. regression guards: L1.5 kinds + L2.1/L2.2 rows unchanged by shape ──
def test_regression_kinds_still_land(tmp_path):
    rows = _rows(tmp_path)
    by = _by_kind(rows)
    imports = sorted(n for n, _ in by.get("import", []))
    assert "os" in imports and "defaultdict" in imports, imports
    params = sorted(n for n, _ in by.get("parameter", []))
    assert "x" in params and "a" in params and "b" in params, params
    # L2.2 kwarg full-volume still lands (the fixture's outer(inner(x=1))):
    kw = sorted(n for n, _ in by.get("kwarg_usage", []))
    assert "x" in kw, kw
    classes = sorted(n for n, _ in by.get("class", []))
    assert "Base" in classes and "Child" in classes, classes


# ── 6. no duplicate rows from double-emit (gate + full walk) ─────────────
def test_no_duplicate_rows(tmp_path):
    rows = _rows(tmp_path)
    assert len(rows) == len(set(rows)), (
        f"{len(rows) - len(set(rows))} duplicate rows — the conservative "
        "gate and the full walk must not both emit the same call")
