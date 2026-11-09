# AI Agent Prompt: Extend `pyrefactor` CLI & Harden Edge-Case Handling

You are a senior Python engineer working on **`pyrefactor`**, a tool that consolidates a Python module, package, or project into a single dependency-ordered file (`_consolidated.py` + `__init__.py`). The existing architecture is: `cli.py` → `core.py` orchestrates `discovery.py` → `parser.py` (tree-sitter, parallel) → `resolver.py` (collision/dedupe/rename) → `ordering.py` (topo sort) → `writer.py`. Imports are consolidated via `imports.py`; logging via `logging_setup.py`.

Your task is to **propose and, where appropriate, implement** (a) additional CLI features/flags and (b) robust handling for edge cases. Keep changes backward-compatible, preserve existing `--mode single` behavior, and keep the parser/resolver decoupled.

## Deliverables

1. **New flags/features** — for each, specify: flag name, values/default, semantics, which module it touches (`cli.py`, `core.py`, `discovery.py`, `resolver.py`, `writer.py`, etc.), and whether it is a breaking change.
2. **Edge-case handling plan** — for each case: describe detection point (parser/resolver/discovery), desired behavior, warning vs. error, and whether it should be gated behind a flag.
3. **Tests** — list concrete test cases (unit + integration) for every new feature and edge case.
4. **Docs** — concise `--help` text and README sections.

## Features / Flags to Add

- `--dry-run` / `--check`: show the plan or verify no changes are needed; never write files.
- `--stdout`: write consolidated output to stdout instead of files.
- `--config pyproject.toml`: persist excludes, entry modules, formatting, strictness settings.
- `--format` / `--post-format`: run `black`/`ruff format` on generated files.
- `--diff`: emit unified diff against existing output.
- `--overwrite` / `--force`: control overwriting non-empty output directories.
- `--include GLOB` plus defined precedence rules vs. `--exclude`.
- `--keep-docstrings` / `--strip-comments` / `--keep-comments`.
- `--rename-strategy {prefix,suffix,module_path,error}`: collision policy.
- `--dedupe {none,safe,aggressive}`: control duplicate-block removal.
- `--imports {prune,keep,strict}`: supersede/refine `--keep-unused-imports`.
- `--all {ignore,respect,merge,recompute}`: richer `__all__` handling than `--respect-all`.
- `--entry` improvements: keep multiple mains, rename `main`, optionally auto-generate an `if __name__ == "__main__":` guard.
- `--namespace` / `--preserve-modules`: emit module-like namespaces or origin comments.
- `--source-map`: inline comments mapping each block to its original file/line range.
- `--fail-on-warning`, `--max-skipped N`, `--allow-syntax-errors`.
- `--jobs` alias; `--no-parallel`; progress bar.
- `--validate`: compile output and optionally smoke-import it.
- `--mode {single,package,module,flat,split}`: additional output layouts (must keep `single` default).
- `--pyproject` / `--setup-cfg` awareness for package discovery.

## Edge Cases to Handle

**Dynamic & metaprogramming**
- `importlib`, `__import__`, `globals()`, `locals()`, `eval`, `exec`, `getattr` used for imports or name lookup.
- `__getattr__`, module-level `__dir__`, monkeypatching.

**Exports & imports**
- Computed or mutated `__all__`; re-exports; local and external star imports.
- Conditional/platform/optional imports; `TYPE_CHECKING` imports.
- Nested imports inside functions/classes; side-effect-only imports.
- Import aliases; collisions between external and local names.
- Circular imports and circular import-time dependencies.

**Names & scoping**
- Duplicate definitions with identical or differing bodies; duplicate side-effect blocks.
- Class-body scoping, comprehensions, lambdas, walrus operator, `match`, `global`/`nonlocal`.
- Decorators, type annotations, default arguments referencing renamed names.
- Module-scope rebindings and shadowing by parameters/locals.

**Runtime semantics**
- `__file__`, `__name__`, `__package__`, `__path__`, relative paths, resource loading.
- Imports that depend on module identity or import order.
- `__main__` guards; multiple entry modules; `main()` detection.

**Discovery & IO**
- Namespace packages, `src/` layout, single-file input, symlinks, output inside source tree.
- Syntax errors, partial parse, encoding/BOM, CRLF, tabs, shebang/encoding cookies.
- `pyproject.toml`/`setup.py` discovery; exclusion of tests/docs; vendored/generated code.
- Huge projects; worker crash/pickling; permission errors; non-UTF8 files.

## Constraints

- Prefer **warnings + skip** for unrecoverable per-file issues; only fail hard when `--strict`, `--fail-on-warning`, or `--validate` demands it.
- Never silently drop code or imports; log every rename, dedupe, and skipped file at `INFO`/`WARNING`.
- Preserve the `ConsolidationResult` contract; add fields rather than changing existing ones.
- Keep worker-parsed data picklable (`models.py` dataclasses only).
- Output must compile (`compile(text, fname, "exec")`); `--validate` should also attempt `import`.
- Document all new flags in `build_parser()` help strings and in the README.

## Output Format

Respond with:
1. A short summary of proposed changes.
2. A table of **new flags** (`flag | default | effect | module | breaking?`).
3. A table of **edge cases** (`case | detection | behavior | gated by`).
4. A prioritized implementation plan (P0/P1/P2).
5. A test matrix.
6. Any open questions or risks.