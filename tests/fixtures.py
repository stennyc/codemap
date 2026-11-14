"""Fixtures: per-language source snippets whose call sites are known.

Each file contains a bare call AND a receiver-qualified call so the L2
call_site branch is exercised on both shapes per language.  The expected
`call_site` names are asserted in test_call_sites.py.
"""

# python
PYTHON_SRC = b"""import os
def f():
    g(1)
    obj.m()
    print(2)
"""
PYTHON_EXPECTED_CALL_NAMES = {"g", "m", "print"}

# typescript
TYPESCRIPT_SRC = b"""function g(x: number) {}
g(2)
console.log(3)
"""
TYPESCRIPT_EXPECTED_CALL_NAMES = {"g", "log"}

# javascript
JAVASCRIPT_SRC = b"""function g(x) {}
g(2)
console.log(3)
"""
JAVASCRIPT_EXPECTED_CALL_NAMES = {"g", "log"}

# go
GO_SRC = b"""package main
func f() {
    g(1)
    fmt.Println(2)
}
"""
GO_EXPECTED_CALL_NAMES = {"g", "Println"}

# rust (println! is a macro_invocation, deliberately NOT a call_site row)
RUST_SRC = b"""fn main() {
    g(1);
    obj.m();
}
"""
RUST_EXPECTED_CALL_NAMES = {"g", "m"}

# java
JAVA_SRC = b"""public class X {
    void f() { g(1); obj.m(); System.out.println(2); }
}
"""
JAVA_EXPECTED_CALL_NAMES = {"g", "m", "println"}

# dart
DART_SRC = b"""void main() {
    g(1);
    obj.m(2);
    print(3);
}
"""
DART_EXPECTED_CALL_NAMES = {"g", "m", "print"}

# c
C_SRC = b"""#include <stdio.h>
int main() { g(1); puts("hi"); return 0; }
"""
C_EXPECTED_CALL_NAMES = {"g", "puts"}

# c++
CPP_SRC = b"""#include <cstdio>
int main() { g(1); obj.m(); std::puts("hi"); return 0; }
"""
CPP_EXPECTED_CALL_NAMES = {"g", "m", "puts"}

# php
PHP_SRC = b'<?php function foo() { bar(); } class Baz { public function qux() { helper(1); } }'
PHP_EXPECTED_CALL_NAMES = {"bar", "helper"}

# kotlin
KOTLIN_SRC = b'fun main() { helper(1) }\nclass Foo { fun bar() { helper(2) } }'
KOTLIN_EXPECTED_CALL_NAMES = {"helper"}


# ---- gate ④ timing-window fixtures ------------------------------------------
# A root containing a single small .py file; used to measure that a
# code_map_query(kind="call_site") round-trip stays under QUERY_GATE_MS.
GATE4_PY_FILE = b"""def helper():
    return 1

def caller():
    helper()
    print(2)
"""
