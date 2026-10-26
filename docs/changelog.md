# Changelog

## Unreleased — prompt.md 3.4 (`nonlocal`) + 5.2 (post-rename dedupe)

- `global`/`nonlocal` declarations are tracked per scope (`parser._scope_declarations`,
  `_nested_nonlocals`, `_function_shadow`): a name declared `nonlocal` keeps the
  same spelling as the enclosing function binding it refers to, instead of being
  renamed on one side and left alone on the other (which silently split one
  variable into two). `global` declarations remove the inherited shadow, so a
  renamed module-level object is written consistently inside the function
  (previously `global x` could stay `x` while the module object became `b_x`).
- A second dedupe pass runs *after* `_assign_names`/`_resolve_ext_conflicts`
  (`resolver._dedupe_post_rename`, keyed on `_block_signature`). It compares
  resolve-to-the-same-thing identities instead of pre-rename text, so blocks
  that differ only by `from .models import User as U` vs `from .models import
  User`, or by an import alias, are recognised as duplicates. It runs to a fixed
  point and the imports that only the dropped duplicate used are pruned.

## Unreleased — prompt.md follow-up: correctness fixes + docstrings

Correctness (silently wrong output before):

- `from __future__` imports are only emitted when *every* module requests them;
  partial usage is reported per module instead of applied to all of them.
- `__init__.py` re-exports such as `from .sub import helper as public_helper`
  are exported (and aliased) by the generated `__init__.py`.
- `__all__` semantics fixed: `=` replaces, `+=`/`.append()`/`.extend()` append,
  an explicit `[]` counts as declared, entries are parsed with `ast.literal_eval`
  (unicode names work, dynamic expressions are reported) and `__all__` is never
  emitted as a constant block.
- Metadata dunders (`__version__`, `__author__`, `__email__`, `__license__`,
  `__copyright__`) and anything listed in `__all__` are exported.
- Non-import statements inside `if TYPE_CHECKING:` are preserved instead of
  dropped; internal imports there are hoisted out of the guard.
- Internal imports inside `try/except ImportError` guards are hoisted to module
  level (with a warning) instead of producing broken output; imports that cannot
  be resolved (compiled extensions) are kept verbatim instead of dropped.
- `--generate-guard` emits `asyncio.run(main())` for `async def main`.
- Local bindings (`for`, `with`, `except ... as`, `match` captures, comprehension
  and walrus targets) are no longer rewritten by module-level renames; class
  references inside `match` patterns still are.
- Non-UTF8 sources honour PEP 263 cookies, are re-encoded as utf-8, and the stale
  coding cookie is rewritten.
- Duplicate constant definitions are deduplicated in `safe` mode; imports inside
  function bodies stay local so optional dependencies stay optional.

Safety / IO:

- Using a project module as a value (e.g. `print(utils)`) is a hard error instead
  of a silent `NameError`; string forward references to renamed objects warn.
- `--strict` promotes unresolved-name warnings to errors and rejects import
  cycles; `--check` fails when the output is missing.
- A file at the output path fails with a clear message; symlinks are deduplicated
  by resolved path; `__main__.py` exclusion is announced; `--eol` was added;
  `--validate-import` warns that it executes user code.
- New warnings for `pickle`/`logging.getLogger(__name__)`/`__file__`-relative
  resources, star-import shadowing, and overlapping rewrites (logged at DEBUG).

Docs/tests: Google-style docstrings for every module, class and function across
`src/`, `tests/`, `tools/` and `examples/` (`ruff check --select D` is clean),
plus `tests/test_edge_cases.py` covering each fix.

## 1.4.8 — Prompt.md Full + Tests/Examples/Docs

- Implements all `prompt.md` flags (dry-run, stdout, config, format, diff, overwrite, include, docstrings/comments, rename-strategy, dedupe, imports, all, entry, namespace, source-map, fail-on-warning, max-skipped, allow-syntax-errors, jobs, validate, modes, pyproject awareness).
- Edge-case hardening (dynamic, __all__, star, conditional, circular, scoping, runtime, discovery/IO).
- Data-file preservation (`discover_data_files` → copy).
- Added `tests/` (pytest 50+ cases), `examples/` (basic/advanced/programmatic/data/config), `docs/` (mkdocs Material + mkdocstrings), documented every project file.

## 1.4.7

- Initial AI-generated `pyrefactor` (`single` mode, tree-sitter, parallel, topo ordering, collision rename prefix).

## 1.4.8-pre

- Added `pyproject.toml` `[tool.pyrefactor]` example, `src/refactor_utils/data/` examples.
