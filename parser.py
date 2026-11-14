"""tree-sitter symbol extraction for Python, TypeScript, JavaScript, Go, Rust, Java, Dart, C, C++, PHP, Kotlin.

Contract gate ①: streaming, O(single-file) peak memory.

`parse_file` extracts every top-level/import symbol plus class methods from a
single source file and returns a list of `(name, kind, line, sig)` rows. It
never reads more than one file into the symbol pipeline at a time, and releases
the tree as soon as the rows are collected — so peak memory is O(one file),
never O(directory).

Kinds emitted (per-language):
  Python:    class, function, method, import
  TypeScript: class, function, method, import, interface
  JavaScript: class, function, method, import
  Go:        function, method, import, type
  Rust:      function, method, import, struct, enum, trait
  Java:      class, function, method, import
  Dart:      class, function, method, import
  C:         function, macro, typedef
  C++:       function, method, class, import (using)

No LSP: names are emitted as-is. Reverse-references elsewhere resolve by
symbol NAME only (alias-import, overloads, dynamic dispatch are NOT
disambiguated — the L1.5 honest upper bound, disclosed in payloads).

L2 addition: call-site edges.  `kind="call_site"` rows record every bare
call expression (``lhs(...)``) and method/qualified call (``receiver.lhs(...)``)
so that "who calls X" is answerable by symbol-name lookup, not just by import
edges.  This is a NAME-level approximation (same class of approximation as
the import rows — alias-shadowing and dynamic dispatch are not disambiguated)
and is disclosed in the query payload alongside the L1.5 disclosure line.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

# Lazy grammar loading — each language loaded on first use.
_GRAMMARS: dict[str, object] = {}
_LANGUAGES: dict[str, object] = {}
_PARSERS: dict[str, object] = {}
_LANG_TYPES: dict[str, tuple[str, ...]] = {}  # language -> (attr_names to try for Language())

SymbolRow = Tuple[str, str, int, str]  # (name, kind, line, sig)


def _lazy_import(lang: str) -> bool:
    """Import and cache the tree-sitter grammar for `lang`. Returns True on success."""
    if lang in _GRAMMARS:
        return True
    try:
        if lang == "python":
            import tree_sitter_python
            _GRAMMARS[lang] = tree_sitter_python
            _LANG_TYPES[lang] = ("language",)
        elif lang == "typescript":
            import tree_sitter_typescript
            _GRAMMARS[lang] = tree_sitter_typescript
            _LANG_TYPES[lang] = ("language_typescript", "language_tsx")
        elif lang == "javascript":
            import tree_sitter_javascript
            _GRAMMARS[lang] = tree_sitter_javascript
            _LANG_TYPES[lang] = ("language",)
        elif lang == "go":
            import tree_sitter_go
            _GRAMMARS[lang] = tree_sitter_go
            _LANG_TYPES[lang] = ("language",)
        elif lang == "rust":
            import tree_sitter_rust
            _GRAMMARS[lang] = tree_sitter_rust
            _LANG_TYPES[lang] = ("language",)
        elif lang == "java":
            import tree_sitter_java
            _GRAMMARS[lang] = tree_sitter_java
            _LANG_TYPES[lang] = ("language",)
        elif lang == "dart":
            import tree_sitter_dart
            _GRAMMARS[lang] = tree_sitter_dart
            _LANG_TYPES[lang] = ("language",)
        elif lang == "c":
            import tree_sitter_c
            _GRAMMARS[lang] = tree_sitter_c
            _LANG_TYPES[lang] = ("language",)
        elif lang == "cpp":
            import tree_sitter_cpp
            _GRAMMARS[lang] = tree_sitter_cpp
            _LANG_TYPES[lang] = ("language",)
        elif lang == "php":
            import tree_sitter_php
            _GRAMMARS[lang] = tree_sitter_php
            _LANG_TYPES[lang] = ("language_php",)
        elif lang == "kotlin":
            import tree_sitter_kotlin
            _GRAMMARS[lang] = tree_sitter_kotlin
            _LANG_TYPES[lang] = ("language",)
        else:
            return False
        from tree_sitter import Language, Parser
        mod = _GRAMMARS[lang]
        lang_fn = None
        for attr_name in _LANG_TYPES[lang]:
            if hasattr(mod, attr_name):
                lang_fn = getattr(mod, attr_name)
                break
        if lang_fn is None:
            return False
        _LANGUAGES[lang] = Language(lang_fn())
        _PARSERS[lang] = Parser(_LANGUAGES[lang])
        return True
    except Exception:
        return False


def _parser_for(lang: str) -> Optional["Parser"]:
    """Get or create a parser for `lang`. Returns None if grammar unavailable."""
    if lang not in _PARSERS:
        if not _lazy_import(lang):
            return None
    return _PARSERS.get(lang)


def _sig_for(node) -> str:
    """Compact one-line signature from the node (bounded, never the body)."""
    text = node.text
    if text is None:
        return ""
    first = text.split(b"\n", 1)[0]
    s = first.decode("utf-8", "replace").strip()
    if len(s) > 100:
        s = s[:97] + "..."
    return s


def _detect_language(path: str) -> Optional[str]:
    """Detect language from file extension."""
    ext = Path(path).suffix.lower()
    from .registry import EXT_TO_LANG
    return EXT_TO_LANG.get(ext)


def _extract_name(child) -> Optional[str]:
    """Extract text name from a child node, handling various node types."""
    if child is None:
        return None
    # Try field_name first
    fn = child.child_by_field_name("name")
    if fn is not None:
        return fn.text.decode("utf-8", "replace")
    # Fall back to direct identifier
    if child.type in ("identifier", "type_identifier", "string"):
        return child.text.decode("utf-8", "replace")
    # Look for identifier child
    for sub in child.children:
        if sub.type in ("identifier", "type_identifier"):
            return sub.text.decode("utf-8", "replace")
    return None


def _call_site_row(node, in_class: bool, lang: str) -> Optional[SymbolRow]:
    """Extract a call_site row from a call-ish node, or None.

    Rule: take the LAST identifier-type child of the LHS ("function"/"name"
    field, or the node itself for java/dart) — this is the called symbol name
    for both bare calls (`g()`) and receiver-qualified calls (`obj.m()` →
    "m", `std::puts()` → "puts", `System.out.println()` → "println").  Pure
    NAME-level; receiver/namespace qualification is not resolved into a
    fully-qualified symbol, matching the import-row approximation class.

    Dart has no call-named node: a call sits under an `expression_statement`
    as `identifier + selector(argument_part)`, handled in its own branch.
    """
    if lang == "dart":
        # expression_statement wraps: identifier (base) + selector
        #   (unconditional_assignable_selector `.m` + argument_part `(args)`)
        # A row is emitted only when a `(` argument_part is actually present,
        # i.e. `g(1)` / `obj.m(2)` — a bare `obj.m` selector without args is
        # not a call and yields no row.
        base_name = None
        has_args = False
        for sub in node.children:
            if sub.type == "identifier":
                base_name = sub.text.decode("utf-8", "replace")
            elif sub.type == "selector":
                for ssub in sub.children:
                    if ssub.type in ("argument_part", "unconditional_assignable_selector"):
                        has_args = True
                    if ssub.type == "unconditional_assignable_selector":
                        # `.m` — the called member name is the last identifier under it
                        for ssss in ssub.children:
                            if ssss.type == "identifier":
                                base_name = ssss.text.decode("utf-8", "replace")
        if not has_args or base_name is None:
            return None
        return (base_name, "call_site", node.start_point[0] + 1, _sig_for(node))

    # python / typescript / javascript / go / rust / c / cpp / java
    fn = node.child_by_field_name("function")
    if fn is None:
        fn = node.child_by_field_name("name")  # java method_invocation
    if fn is None:
        # PHP: function_call_expression has a `name` CHILD (not a field)
        # Kotlin: call_expression has an `identifier` child
        for sub in node.children:
            if sub.type in ("name", "identifier"):
                fn = sub
                break
    if fn is None:
        return None

    # Collect all identifier-type children, then take the LAST one (the called
    # name, not the receiver/namespace qualifier).
    id_children = [c for c in fn.children if c.type in (
        "identifier", "property_identifier", "field_identifier",
        "type_identifier", "namespace_identifier", "field_identifier")]
    if fn.type in ("identifier", "property_identifier", "field_identifier",
                   "type_identifier"):
        id_children = [fn]
    if not id_children:
        # Leaf name nodes (e.g. PHP `name` child of `function_call_expression`)
        # have no identifier-type children — use the node's own text directly.
        if fn.text:
            nm = fn.text.decode("utf-8", "replace").strip()
            if nm:
                return (nm, "call_site", node.start_point[0] + 1, _sig_for(node))
        return None
    nm = id_children[-1].text.decode("utf-8", "replace")
    if not nm:
        return None
    return (nm, "call_site", node.start_point[0] + 1, _sig_for(node))


def _symbol_rows_from_node(node, in_class: bool = False, lang: str = "python") -> List[SymbolRow]:
    """Recurse a tree node, emitting symbol rows for the MVP kind set."""
    rows: List[SymbolRow] = []
    for child in node.children:
        ctype = child.type
        # L2 call-site edges: check before any language-specific dispatch so a
        # call node inside a block / expression_statement is captured regardless
        # of surrounding structure.  Dart's call shape has no "call"-named node
        # (expression_statement + identifier + selector/argument_part), so it is
        # handled in the `lang == "dart"` branch just below.
        _call_node_types = (
            "call",                # python
            "call_expression",    # typescript, javascript, go, rust, c, cpp, kotlin
            "method_invocation",   # java
            "function_call_expression",  # php
            "member_call_expression",    # php
        )
        if ctype in _call_node_types or (lang == "dart" and ctype == "expression_statement"):
            row = _call_site_row(child, in_class, lang)
            if row is not None:
                rows.append(row)
            continue

        # decorated_definition wraps the real def/class; unwrap one level.
        if ctype == "decorated_definition":
            rows.extend(_symbol_rows_from_node(child, in_class, lang))
            continue

        # ---- Python/TS/JS/Java/Dart: function_definition / class_definition ----
        if lang in ("python", "typescript", "javascript", "java", "dart"):
            # TS/JS use function_declaration (not function_definition)
            if ctype == "function_declaration":
                name = _extract_name(child)
                if name:
                    kind = "method" if in_class else "function"
                    rows.append((name, kind, child.start_point[0] + 1, _sig_for(child)))
                for sub in child.children:
                    if sub.type in ("statement_block", "body", "block"):
                        rows.extend(_symbol_rows_from_node(sub, in_class, lang))
                        break
                continue
            if ctype == "function_definition":
                name = _extract_name(child)
                if name:
                    kind = "method" if in_class else "function"
                    rows.append((name, kind, child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is None:
                    body = child.child_by_field_name("body")
                if body is None:
                    # TS/JS may not have field names; try direct child
                    for sub in child.children:
                        if sub.type in ("block", "{", "}"):
                            body = sub
                            break
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, in_class, lang))
                continue
            if ctype == "class_definition":
                name = _extract_name(child)
                if name:
                    rows.append((name, "class", child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, in_class=True, lang=lang))
                continue
            # TS/JS class_declaration
            if ctype == "class_declaration":
                name = _extract_name(child)
                if name:
                    rows.append((name, "class", child.start_point[0] + 1, _sig_for(child)))
                for sub in child.children:
                    if sub.type in ("class_body", "body"):
                        rows.extend(_symbol_rows_from_node(sub, in_class=True, lang=lang))
                        break
                continue
            # TS interface_declaration
            if ctype == "interface_declaration":
                name = _extract_name(child)
                if name:
                    rows.append((name, "interface", child.start_point[0] + 1, _sig_for(child)))
                continue
            if ctype in ("import_statement", "import_from_statement"):
                line = child.start_point[0] + 1
                sig = _sig_for(child)
                after_kw = False
                for sub in child.children:
                    if sub.type == "import":
                        after_kw = True
                        continue
                    if not after_kw:
                        continue
                    # Handle nested import structures (TS/JS named imports)
                    if sub.type in ("import_specifier", "identifier", "dotted_name", "aliased_import"):
                        if sub.type == "aliased_import":
                            alias = sub.child_by_field_name("alias")
                            nm = alias.text.decode("utf-8", "replace") if alias is not None else sub.text.decode("utf-8", "replace")
                        elif sub.type == "import_specifier":
                            # Get the identifier inside
                            idents = [c for c in sub.children if c.type == "identifier"]
                            nm = idents[0].text.decode("utf-8", "replace") if idents else sub.text.decode("utf-8", "replace")
                        else:
                            nm = sub.text.decode("utf-8", "replace")
                        if nm:
                            rows.append((nm, "import", line, sig))
                    # Recurse into import_clause/named_imports for nested structures
                    elif sub.type in ("import_clause", "named_imports"):
                        for ssub in sub.children:
                            if ssub.type == "import_specifier":
                                idents = [c for c in ssub.children if c.type == "identifier"]
                                nm = idents[0].text.decode("utf-8", "replace") if idents else ssub.text.decode("utf-8", "replace")
                                if nm:
                                    rows.append((nm, "import", line, sig))
                            elif ssub.type == "named_imports":
                                # TS/JS: import_clause -> named_imports -> import_specifier
                                for ssss in ssub.children:
                                    if ssss.type == "import_specifier":
                                        idents = [c for c in ssss.children if c.type == "identifier"]
                                        nm = idents[0].text.decode("utf-8", "replace") if idents else ssss.text.decode("utf-8", "replace")
                                        if nm:
                                            rows.append((nm, "import", line, sig))
                            elif ssub.type == "identifier":
                                nm = ssub.text.decode("utf-8", "replace")
                                if nm:
                                    rows.append((nm, "import", line, sig))
                continue
            if ctype in ("module", "block", "class_body",
                         "program", "function_body",
                         "statement_block", "statement_list"):
                # program/function_body/statement_block/statement_list added for
                # L2: dart's call-bearing expression_statements sit inside
                # function_body>block; ts/js call_expressions inside
                # statement_block; rust call_expressions inside blocks under
                # source_file (block already covered by L627 fallback).
                rows.extend(_symbol_rows_from_node(child, in_class, lang))
                continue

        # ---- Go ----
        if lang == "go":
            if ctype == "function_declaration":
                name = _extract_name(child)
                if name:
                    rows.append((name, "function", child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, in_class, lang))
                continue
            if ctype == "method_declaration":
                name = _extract_name(child)
                if name:
                    rows.append((name, "method", child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, True, lang))
                continue
            if ctype == "import_declaration":
                line = child.start_point[0] + 1
                sig = _sig_for(child)
                # Go: import "fmt" -> import_spec -> interpreted_string_literal -> interpreted_string_literal_content
                for sub in child.children:
                    if sub.type == "import_spec":
                        for ssub in sub.children:
                            if ssub.type == "interpreted_string_literal":
                                for ssss in ssub.children:
                                    if ssss.type == "interpreted_string_literal_content":
                                        nm = ssss.text.decode("utf-8", "replace").strip()
                                        if nm:
                                            rows.append((nm, "import", line, sig))
                            elif ssub.type in ("identifier", "dotted_identifier", "basic_literal"):
                                nm = sub.text.decode("utf-8", "replace").strip()
                                if nm and nm != "import":
                                    rows.append((nm, "import", line, sig))
                    elif sub.type in ("identifier", "dotted_identifier", "basic_literal"):
                        nm = sub.text.decode("utf-8", "replace").strip()
                        if nm and nm != "import":
                            rows.append((nm, "import", line, sig))
                continue
            if ctype == "type_declaration":
                # Go: type MyType struct{}
                for sub in child.children:
                    if sub.type == "type_spec":
                        idents = [c for c in sub.children if c.type == "type_identifier"]
                        if idents:
                            rows.append((idents[0].text.decode("utf-8"), "type", idents[0].start_point[0] + 1, _sig_for(idents[0])))
                continue
            if ctype in ("source_file", "program", "block", "statement_list", "expression_statement"):
                # block/statement_list/expression_statement added for L2: Go
                # call_expression nodes sit inside expression_statement inside a
                # statement_list inside a function body block
                rows.extend(_symbol_rows_from_node(child, in_class, lang))
                continue

        # ---- PHP ----
        if lang == "php":
            if ctype == "function_definition":
                name = _extract_name(child)
                if name:
                    rows.append((name, "function", child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is None:
                    for sub in child.children:
                        if sub.type == "compound_statement":
                            body = sub
                            break
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, in_class, lang))
                continue
            if ctype == "class_declaration":
                name = _extract_name(child)
                if name:
                    rows.append((name, "class", child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is None:
                    for sub in child.children:
                        if sub.type in ("declaration_list", "class_body"):
                            body = sub
                            break
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, True, lang))
                continue
            if ctype == "method_declaration":
                name = _extract_name(child)
                if name:
                    rows.append((name, "method", child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is None:
                    for sub in child.children:
                        if sub.type == "compound_statement":
                            body = sub
                            break
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, True, lang))
                continue
            if ctype in ("namespace_use_declaration", "use_declaration", "import_declaration"):
                line = child.start_point[0] + 1
                sig = _sig_for(child)
                for sub in child.children:
                    if sub.type in ("name", "qualified_name", "scoped_name"):
                        for iden in sub.children:
                            if iden.type == "name":
                                nm = iden.text.decode("utf-8", "replace").strip()
                                if nm:
                                    rows.append((nm, "import", line, sig))
                                    break
                continue
            if ctype in ("program", "compound_statement", "declaration_list", "expression_statement"):
                rows.extend(_symbol_rows_from_node(child, in_class, lang))
                continue

        # ---- Kotlin ----
        if lang == "kotlin":
            if ctype == "function_declaration":
                name = _extract_name(child)
                if name:
                    kind = "method" if in_class else "function"
                    rows.append((name, kind, child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is None:
                    for sub in child.children:
                        if sub.type in ("function_body", "block"):
                            body = sub
                            break
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, in_class, lang))
                continue
            if ctype == "class_declaration":
                name = _extract_name(child)
                if name:
                    rows.append((name, "class", child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is None:
                    for sub in child.children:
                        if sub.type in ("class_body", "block"):
                            body = sub
                            break
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, True, lang))
                continue
            if ctype in ("source_file", "block", "class_body", "function_body"):
                rows.extend(_symbol_rows_from_node(child, in_class, lang))
                continue

        # ---- Rust ----
        if lang == "rust":
            if ctype == "function_item":
                name = _extract_name(child)
                if name:
                    rows.append((name, "function", child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, in_class, lang))
                continue
            if ctype == "impl_item":
                for sub in child.children:
                    if sub.type in ("identifier", "type_identifier"):
                        pass  # type name is parent's job
                items = child.child_by_field_name("items")
                if items is None:
                    for sub in child.children:
                        if sub.type == "declaration_list":
                            items = sub
                            break
                if items is not None:
                    rows.extend(_symbol_rows_from_node(items, True, lang))
                continue
            if ctype in ("struct_item", "enum_item", "trait_item"):
                name = _extract_name(child)
                if name:
                    kind = "struct" if ctype == "struct_item" else ("enum" if ctype == "enum_item" else "trait")
                    rows.append((name, kind, child.start_point[0] + 1, _sig_for(child)))
                continue
            if ctype in ("use_declaration",):
                line = child.start_point[0] + 1
                sig = _sig_for(child)
                # Rust: use std::collections::HashMap -> scoped_identifier -> identifiers
                for sub in child.children:
                    if sub.type == "scoped_identifier":
                        idents = [c for c in sub.children if c.type == "identifier"]
                        if idents:
                            nm = idents[-1].text.decode("utf-8", "replace").strip()
                            if nm:
                                rows.append((nm, "import", line, sig))
                    elif sub.type in ("identifier", "use_path_wildcard"):
                        nm = sub.text.decode("utf-8", "replace").strip()
                        if nm:
                            rows.append((nm, "import", line, sig))
                continue
            if ctype in ("source_file", "block"):
                rows.extend(_symbol_rows_from_node(child, in_class, lang))
                continue

        # ---- Java ----
        if lang == "java":
            if ctype == "method_declaration":
                name = _extract_name(child)
                if name:
                    rows.append((name, "method", child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, True, lang))
                continue
            if ctype == "class_declaration":
                name = _extract_name(child)
                if name:
                    rows.append((name, "class", child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, True, lang))
                continue
            if ctype in ("import_declaration",):
                line = child.start_point[0] + 1
                sig = _sig_for(child)
                # Java: import java.util.List; -> scoped_identifier -> last identifier
                for sub in child.children:
                    if sub.type == "scoped_identifier":
                        idents = [c for c in sub.children if c.type == "identifier"]
                        if idents:
                            nm = idents[-1].text.decode("utf-8", "replace").strip()
                            if nm:
                                rows.append((nm, "import", line, sig))
                    elif sub.type in ("identifier",):
                        nm = sub.text.decode("utf-8", "replace").strip()
                        if nm and nm != "import":
                            rows.append((nm, "import", line, sig))
                continue
            if ctype in ("compilation_unit", "class_body", "method_body"):
                rows.extend(_symbol_rows_from_node(child, in_class, lang))
                continue

        # ---- Dart ---- (call-site rows are emitted by the L2 gate at the top of
        # the loop for `expression_statement`; the branches below only add the
        # function_signature/function_body symbol rows that L1.5 already had)
        if lang == "dart" and ctype == "function_signature":
            idents = [c for c in child.children if c.type == "identifier"]
            if idents:
                rows.append((idents[0].text.decode("utf-8"), "function", child.start_point[0] + 1, _sig_for(child)))
            # Also check function_body for methods inside classes
            continue
        if lang == "c":
            if ctype == "function_definition":
                decl = child.child_by_field_name("declarator")
                name = None
                if decl is not None:
                    for sub in decl.children:
                        if sub.type == "identifier":
                            name = sub
                            break
                if name is not None:
                    rows.append((name.text.decode("utf-8"), "function", child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, in_class, lang))
                continue
            if ctype == "preproc_def":
                name = child.child_by_field_name("name")
                if name is None:
                    for sub in child.children:
                        if sub.type == "identifier":
                            name = sub
                            break
                if name is not None:
                    rows.append((name.text.decode("utf-8"), "macro", child.start_point[0] + 1, _sig_for(child)))
                continue
            if ctype == "type_definition":
                # typedef int myint;
                idents = [c for c in child.children if c.type in ("type_identifier", "identifier")]
                if idents:
                    rows.append((idents[-1].text.decode("utf-8"), "typedef", idents[-1].start_point[0] + 1, _sig_for(child)))
                continue
            if ctype in ("translation_unit", "compound_statement"):
                rows.extend(_symbol_rows_from_node(child, in_class, lang))
                continue

        # ---- C++ ----
        if lang == "cpp":
            if ctype == "function_definition":
                # C++ method: void bar() inside class
                decl = child.child_by_field_name("declarator")
                name = None
                if decl is not None:
                    for sub in decl.children:
                        if sub.type in ("identifier", "field_identifier"):
                            name = sub
                            break
                if name is not None:
                    kind = "method" if in_class else "function"
                    rows.append((name.text.decode("utf-8"), kind, child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is None:
                    for sub in child.children:
                        if sub.type == "compound_statement":
                            body = sub
                            break
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, in_class, lang))
                continue
            if ctype == "class_specifier":
                name = child.child_by_field_name("name")
                if name is None:
                    for sub in child.children:
                        if sub.type == "identifier":
                            name = sub
                            break
                if name is not None:
                    rows.append((name.text.decode("utf-8"), "class", child.start_point[0] + 1, _sig_for(child)))
                body = child.child_by_field_name("body")
                if body is not None:
                    rows.extend(_symbol_rows_from_node(body, True, lang))
                continue
            if ctype in ("using_declaration", "import_declaration"):
                line = child.start_point[0] + 1
                sig = _sig_for(child)
                for sub in child.children:
                    if sub.type in ("identifier", "nested_name_specifier"):
                        nm = sub.text.decode("utf-8", "replace").strip()
                        if nm:
                            rows.append((nm, "import", line, sig))
                continue
            if ctype == "preproc_definition":
                name = child.child_by_field_name("name")
                if name is None:
                    for sub in child.children:
                        if sub.type == "identifier":
                            name = sub
                            break
                if name is not None:
                    rows.append((name.text.decode("utf-8"), "macro", child.start_point[0] + 1, _sig_for(child)))
                continue
            if ctype in ("translation_unit", "compound_statement"):
                rows.extend(_symbol_rows_from_node(child, in_class, lang))
                continue

        # Fallback: recurse into compound/statement nodes (expression_statement
        # added for L2: bare call nodes in Python/C-family sit directly under it,
        # not under a container already listed)
        if ctype in ("module", "block", "source_file", "program", "body",
                     "declaration_list", "expression_statement"):
            rows.extend(_symbol_rows_from_node(child, in_class, lang))

    return rows


def parse_bytes(source: bytes, lang: Optional[str] = None) -> List[SymbolRow]:
    """Extract symbol rows from raw source. Pure — no file I/O (tests use this)."""
    p = _parser_for(lang or "python")
    if p is None:
        return []
    tree = p.parse(source)
    try:
        detected = lang or "python"
        return _symbol_rows_from_node(tree.root_node, in_class=False, lang=detected)
    finally:
        tree = None  # release


def parse_file(path: str) -> List[SymbolRow]:
    """Read + parse one file, return its symbol rows. O(single file) memory."""
    source = Path(path).read_bytes()
    lang = _detect_language(path)
    if lang is None:
        return []
    return parse_bytes(source, lang=lang)


def available() -> bool:
    """Whether tree-sitter is usable in this environment."""
    return _parser_for("python") is not None


def available_for(lang: str) -> bool:
    """Whether a specific language grammar is available."""
    return _parser_for(lang) is not None


def supported_languages() -> List[str]:
    """Return list of languages with available grammars."""
    langs = ["python", "typescript", "javascript", "go", "rust", "java", "dart", "c", "cpp"]
    return [l for l in langs if available_for(l)]
