# Pipeline

```text
cli.py -> core.py orchestrates
        -> discovery.py   find sources and data files, read pyproject config
        -> parser.py      tree-sitter, parallel; blocks, imports, guards
        -> resolver.py    dedupe, renames, import resolution, ordering input
        -> ordering.py    topological sort with deterministic tie-breaks
        -> imports.py     merge, sort and render the import section
        -> writer.py      render the single consolidated file
        -> core.validate_syntax   ast check, then write
models.py          picklable dataclasses shared by parser and resolver
logging_setup.py   loguru configuration
```

## 1. Discovery (`discovery.py`)

`discover()` walks the source and returns one `SourceFile` per Python file with
its dotted module name. The base of the dotted name is detected automatically:
a directory with `__init__.py` is a package (its parent is the base), a `src/`
layout is recognised, otherwise the directory itself is the base.

`discover_data_files()` returns the non-Python files worth keeping (`.json`,
`.yaml`, `.toml`, `.ini`, `.csv`, `.txt`, `.md`, `.sql`, ... and well-known
names such as `LICENSE` or `requirements.txt`).

## 2. Parsing (`parser.py`)

Each file is parsed by tree-sitter in a worker process (or thread). A file
with a syntax error aborts the whole run: a partial consolidation is never
emitted.

Top-level statements become `Block` objects:

| Kind | Source construct |
|------|------------------|
| `const` | assignments, type aliases |
| `class` | class definitions (with decorators) |
| `func` | function definitions (with decorators) |
| `misc` | other statements: calls, loops, `with`, mixed compound blocks |
| `main` | `if __name__ == "__main__":` |

Not turned into blocks:

- imports: collected as `ImportSpec`;
- `try/except ImportError` and `if sys.platform ...` import guards: kept
  verbatim as `GuardBlock`;
- `if TYPE_CHECKING:`: flattened, its imports join the normal imports and its
  definitions become ordinary blocks;
- `__all__` statements: read as hints, never emitted;
- module docstrings and other bare string statements: pruned.

Every identifier that might refer to a module-level name is recorded as an
`Occ` (byte range, name, eager or lazy). The resolver uses these to rewrite
references without touching tree-sitter nodes, which keeps worker payloads
picklable.

## 3. Resolution (`resolver.py`)

1. Build one environment per module: local definitions, imports, star imports.
2. Analyse each block: which project blocks and which external imports it uses,
   and whether the use is eager (import time) or lazy (inside a function body).
3. Deduplicate byte-identical definitions.
4. Assign final names. See [Name resolution and renaming](name-resolution.md).
5. Resolve conflicts between external imports that bind the same local name.
6. Rewrite every occurrence and render the blocks.

## 4. Ordering (`ordering.py`)

Blocks are sorted topologically on their eager dependencies, preferring
constants, then classes, then functions, then module order and source order.
A `main` function nobody depends on and the merged `__main__` guard go last.
Cycles are reported with a warning.

## 5. Rendering (`writer.py`)

The file is assembled as: generated docstring, `__future__` imports, imports,
verbatim import guards, then the sections Constants, Classes, Functions and
Entry points. Optionally a formatter is applied.

## 6. Validation and writing (`core.py`)

`validate_syntax()` parses the rendered text with `ast`. Only then is the file
written, through a temporary file that is renamed over the target, and the
data files are copied.
