# code_map

L1.5 code-symbol index for the [Hermes Agent](https://hermes-agent.nousresearch.com) plugin system.
Tree-sitter based, zero external API calls, fully offline.

## What it does

`code_map` builds a **cross-file symbol index** over a confirmed project root, enabling:

| Tool | What it gives you |
|---|---|
| `code_map_query` | Cross-file symbol lookup (name → all definitions across the indexed tree) |
| `code_map_check_root` | Dry-run: how many files would be indexed under this root + path? |
| `code_map_confirm_root` | Build (or refresh) the index for a confirmed target |
| `code_map_refresh` | Authoritative re-scan of a confirmed index target |

**Two-layer retrieval model:**
- **Auto-inject layer** (`pre_llm_call` hook) — injects relevant symbol context each turn.
- **Tool layer** (`code_map_query`) — explicit "I need to locate this symbol now" call.

The two layers complement each other: auto-inject covers what the model *should* already know;
the tool layer covers what the model *explicitly asks for*.

## Supported languages

Python, TypeScript, JavaScript, Go, Rust, Java, Dart, C, C++
(via [tree-sitter](https://tree-sitter.github.io/) grammars)

## Architecture

```
__init__.py       plugin registration + dep-path bootstrap
config.py         limits, path constants, env overrides
db.py             SQLite init / insert / upsert / remove
fingerprint.py    file-level fingerprints, dirty tracking
parser.py         tree-sitter parse → symbol rows (name, kind, line, signature)
query.py          tool handlers: query / confirm / refresh / check_root
state.py          async build lifecycle, root confirmation, commit batching
hook.py           transform_tool_result + post_tool_call hooks (fail-soft)
lockfile.py       per-root file lock (_proc.lock)
rootdetect.py     root marker detection (pyproject.toml, package.json, go.mod, …)
rootreg.py        root registry (confirmed targets)
registry.py       data-dir registry
prune.py          prune stale rows from deleted files
scanner.py        file-walk + estimation
_dep_paths.py     venv bootstrap (tree_sitter + grammars)
```

### Data layout

Index DBs live under `data/<sha1(root).db>` (SQLite, one file per confirmed root).
`data/` is gitignored — regenerable by re-running `code_map_confirm_root`.

## Installation (as a Hermes plugin)

Copy or symlink this directory into `~/.hermes/plugins/code_map/`.
The plugin's `_dep_paths.py` auto-resolves the tree-sitter grammars from
`~/.hermes/plugin-venv`. If that venv does not exist, the plugin
falls back to the host interpreter's own `tree_sitter` + grammar packages
(fail-soft; no crash).

### Dependencies

```
pip install tree-sitter tree-sitter-python tree-sitter-typescript \
            tree-sitter-javascript tree-sitter-go tree-sitter-rust \
            tree-sitter-java tree-sitter-dart tree-sitter-c \
            tree-sitter-cpp
```

## Running the tests

```
cd code_map
python -m pytest tests/ -v
```

All tests run offline; a 5,000-file fixture is generated on-the-fly
in `tests/fixtures.py` for gate-threshold checks.

## License

MIT
