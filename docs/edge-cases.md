# Edge Cases

Full plan from `prompt.md` (see README for table). Each item below is covered by
`tests/test_edge_cases.py`.

## Dynamic & Metaprogramming

- `importlib`, `__import__`, `globals()`, `locals()`, `eval`, `exec`, `getattr` → `parser._detect_dynamic_patterns` warns; consolidation may miss deps → `Warning`.
- `__getattr__`, `__dir__`, monkeypatching → warns.

## Exports & Imports

- Computed/mutated `__all__` → warn, fallback to `ignore`. `=` replaces,
  `+=`/`.append()`/`.extend()` append, `[]` counts as declared, and non-literal
  values are reported instead of guessed (`ast.literal_eval`, not a regex).
- `__all__` never reaches the output as a constant block.
- Re-exports via `__all__` and aliased re-exports in `__init__.py`
  (`from .sub import helper as public_helper`) → `resolver._exports` +
  `Plan.reexports`, rendered as `helper as public_helper`.
- Metadata dunders (`__version__`, …) are exported; other private names are not.
- Star imports → local expanded via `_star_names`; external kept `from pkg import *` + warn.
- Conditional/platform/optional (`sys.platform`, `try/except ImportError`, `TYPE_CHECKING`) → parser detects, guards kept verbatim; internal imports found inside a guard are hoisted to module level and removed from the guard text.
- `TYPE_CHECKING` blocks keep non-import statements (type aliases, `Protocol`
  stubs, `TypeVar`s); names they mention stay imported.
- Unresolvable internal imports (compiled extensions, excluded modules) are kept
  verbatim rather than dropped.
- `from __future__` features are emitted only when *all* modules agree.
- Nested/side-effect-only imports → `nested_import` occ, internal removed, external kept.
- Aliases/collisions → `resolver._assign_names` + `_resolve_ext_conflicts`.

## Names & Scoping

- Duplicate identical bodies → `dedupe safe|aggressive`. A second pass runs
  after renaming (`resolver._dedupe_post_rename`): it fingerprints each block by
  what its references *resolve to* (canonical project block, or the identity of
  the imported object), so blocks that only looked different because of an
  import alias (`from .models import User as U` vs `from .models import User`)
  or a conflict-renamed import are still collapsed, and the imports only those
  duplicates used are pruned. It runs to a fixed point and never merges blocks
  with different names or different bodies.
- Class-body, comprehensions, lambdas, walrus, `match`, `global`/`nonlocal` → `parser._collect_occs` shadow/lazy/cls handling.
- `for` targets, `with ... as`, `except ... as`, `match` captures and
  comprehension variables shadow module-level names, so a rename never touches
  them; the class referenced by a `case Point():` pattern *is* renamed.
- `nonlocal x` is tracked by `parser._function_shadow`: the name is shadowed in
  the enclosing function too, so the local binding, the nested uses and the
  declaration itself keep one spelling (a rename can no longer split the
  variable or point `nonlocal` at a different scope). `global x` removes the
  inherited shadow, so it follows the renamed module-level object instead of
  staying stale behind a `for`/`with` binding.
- Using an imported module as a value (`print(utils)`) is a hard error; string
  forward references to renamed objects produce a warning.
- Decorators/annotations/default args → pushed as `shadow` + `lazy` (future_annotations).

## Runtime

- `__file__`/`__name__`/`__package__`/`__path__` + resource loading → warnings
  (the `__file__` warning points at `--copy-data`).
- `logging.getLogger(__name__)`, `pickle`/`dill`/`shelve`/`marshal` usage →
  warnings about the collapsed logger hierarchy and `__module__` changes.
- Module identity/order → `topo_order` rank.
- `__main__` guards/multi-entry/`main()` → `MAIN` kind, `--entry*`, `tail` mains, `generate_guard` (which uses `asyncio.run` for `async def main`).

## Discovery & IO

- Namespace, `src/` layout, single-file, symlinks (deduplicated by resolved path), output-inside-source → `discovery.py`.
- `__main__.py` is still excluded, but a warning states that `python -m pkg` no
  longer works in the consolidated output.
- An output path that is a file fails with a `ConsolidationError`; `--check`
  fails when there is nothing to compare; `--eol` controls line endings.
- Syntax errors/partial parse → `parser` errors vs `allow_syntax_errors` warnings.
- Encoding/BOM/CRLF/tabs/shebang → parser handles.
- `pyproject.toml`/`setup.py` → `detect_pyproject_config`.
- Huge/worker crash/pickling → `core._parse_all` BrokenExecutor fallback; keep picklable models.
- Permission/non-UTF8 → `cannot read` errors, skipped, `max_skipped`/`strict`.
