# Edge Cases

Full plan from `prompt.md` (see README for table).

## Dynamic & Metaprogramming

- `importlib`, `__import__`, `globals()`, `locals()`, `eval`, `exec`, `getattr` → `parser._detect_dynamic_patterns` warns; consolidation may miss deps → `Warning`.
- `__getattr__`, `__dir__`, monkeypatching → warns.

## Exports & Imports

- Computed/mutated `__all__` → warn, fallback to `ignore`.
- Re-exports via `__all__` → resolver `_exports` resolves.
- Star imports → local expanded via `_star_names`; external kept `from pkg import *` + warn.
- Conditional/platform/optional (`sys.platform`, `try/except ImportError`, `TYPE_CHECKING`) → parser detects, guards kept verbatim.
- Nested/side-effect-only imports → `nested_import` occ, internal removed, external kept.
- Aliases/collisions → `resolver._assign_names` + `_resolve_ext_conflicts`.

## Names & Scoping

- Duplicate identical bodies → `dedupe safe|aggressive`.
- Class-body, comprehensions, lambdas, walrus, `match`, `global`/`nonlocal` → `parser._collect_occs` shadow/lazy/cls handling.
- Decorators/annotations/default args → pushed as `shadow` + `lazy` (future_annotations).

## Runtime

- `__file__`/`__name__`/`__package__`/`__path__` + resource loading → warnings.
- Module identity/order → `topo_order` rank.
- `__main__` guards/multi-entry/`main()` → `MAIN` kind, `--entry*`, `tail` mains, `generate_guard`.

## Discovery & IO

- Namespace, `src/` layout, single-file, symlinks, output-inside-source → `discovery.py`.
- Syntax errors/partial parse → `parser` errors vs `allow_syntax_errors` warnings.
- Encoding/BOM/CRLF/tabs/shebang → parser handles.
- `pyproject.toml`/`setup.py` → `detect_pyproject_config`.
- Huge/worker crash/pickling → `core._parse_all` BrokenExecutor fallback; keep picklable models.
- Permission/non-UTF8 → `cannot read` errors, skipped, `max_skipped`/`strict`.
