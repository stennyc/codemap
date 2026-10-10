"""registry.py — language grammar availability registry (contract ③).

Tracks which tree-sitter grammars are actually installed and usable in the
current environment. Used by the build path to skip languages whose grammar
is missing (fail-soft, not a crash).
"""
from __future__ import annotations

from typing import Dict, List, Set

# The 9 supported languages, keyed by the grammar module name.
SUPPORTED_LANGUAGES: Dict[str, str] = {
    "python": "tree_sitter_python",
    "typescript": "tree_sitter_typescript",
    "javascript": "tree_sitter_javascript",
    "go": "tree_sitter_go",
    "rust": "tree_sitter_rust",
    "java": "tree_sitter_java",
    "dart": "tree_sitter_dart",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
}

# Extension → language mapping (kept here as the source of truth;
# config.INDEXED_EXTENSIONS is derived from this for pruning).
EXT_TO_LANG: Dict[str, str] = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".dart": "dart",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hxx": "cpp", ".hh": "cpp",
}

# Languages that are *conditionally* included (may not be installed).
OPTIONAL_LANGS: Set[str] = {"dart", "c", "cpp"}


def available_languages() -> List[str]:
    """Return the list of languages whose grammar is actually importable."""
    available: List[str] = []
    for lang, module_name in SUPPORTED_LANGUAGES.items():
        try:
            __import__(module_name)
            available.append(lang)
        except ImportError:
            pass
    return available


def language_for_extension(ext: str) -> str:
    """Return the language string for a file extension (or 'unknown')."""
    return EXT_TO_LANG.get(ext.lower(), "unknown")
