# Project Files

Every file in `src/refactor_utils/` and top-level.

## `pyproject.toml`

- `[build-system]` setuptools.
- `[project]` name `refactor_utils`, version `1.4.8`, deps `loguru`, `tree-sitter`, `tree_sitter_python`; optional `[format]`, `[progress]`, `[test]`, `[docs]`, entry point `pyrefactor = refactor_utils.cli:main`.
- `[tool.setuptools]` where=`src`, package-data `data/*`.
- `[tool.pyrefactor]` default config for this repo (exclude `tests/*`, `iwgfyrkugf.py`).
- `[tool.pytest]` `testpaths = ["tests"]`, `pythonpath = ["src"]`.
- `[tool.black]`/`[tool.ruff]`/`[tool.coverage]`/`[tool.mkdocstrings]`.

## `src/refactor_utils/__init__.py`

Public API: re-exports `ConsolidationError`, `ConsolidationResult`, `consolidate` from `core.py`, plus `__version__`.

## `src/refactor_utils/cli.py`

`build_parser()` defines all flags with help referencing module; `main()` resolves aliases (`--jobs`→`--workers`, `--progress-bar`, `--keep-all-mains`, `--post-format`), calls `consolidate()`, handles `ConsolidationError` (exit 2), `dry-run`/`check` reporting, `skipped`/`fail-on-warning`/`validate` exit codes.

## `src/refactor_utils/core.py`

Orchestrates `discover → _parse_all (parallel) → Resolver → writer`. Key: `_parse_worker` (picklable), `_parse_all` (Process/Thread pool with BrokenExecutor fallback, tqdm), `_load_config_file`, `ConsolidationResult` (adds `warnings`, `diff`, `stdout_text`, `copied_data_files`, `validated`, `dry_run`, `plan`), `consolidate()` (handles every flag, modes `single|package|module|flat|split`, data copy via `shutil.copy2`, diff, validate).

## `src/refactor_utils/discovery.py`

- `EXCLUDED_DIRS` (`.git`, `__pycache__`, `venv`, `build`, `tests`, `docs`, …)
- `EXCLUDED_FILE_GLOBS` (`__main__.py`, `conftest.py`, `test_*.py`, `*_test.py`, `setup.py`, `iwgfyrkugf.py`, `_consolidated.py`)
- `DATA_EXTENSIONS`/`DATA_FILENAMES` (json/yaml/csv/txt/…/LICENSE)
- `detect_base()` — handles `__init__.py` parent and `src/` layout.
- `_is_excluded()` — precedence `default < include < exclude`.
- `_walk()` — iterative, symlink-aware, debug logs.
- `discover()` — returns `list[SourceFile(path, module, is_pkg)]` with `detect_base` and `relative_to(base)`, handles single-file, namespace (no `__init__.py`), output-inside-source skip.
- `discover_data_files()` — non-py data discovery, respects same filters, returns `DataFile(path, relative)`.
- `detect_pyproject_config()` — finds `pyproject.toml` upwards, loads `tomllib`/`tomli` `[tool.pyrefactor]`.

## `src/refactor_utils/parser.py`

Tree-sitter based `parse_module_file(path_str, module, is_pkg, keep_docstrings, strip_comments, keep_comments, allow_syntax_errors)` → `ModuleInfo`.

- `_get_parser()` thread-local `Parser(Language(tree_sitter_python))`.
- `parse_import()`, `_resolve_relative()`, `_dotted()`, `_binding_names()`, `_param_names()`, `_lhs_names()`, `_target_names()`, `_bound_names()`, `_scan()`, `_extent()`, `_first_error_line()`, `_collect_occs()` (identifier/attribute, import nested, function/lambda/class collection with shadow/lazy, class body block handling), `_detect_dynamic_patterns`/`_detect_runtime_semantics` (importlib, eval, __file__, etc.), `_strip_docstring_from_source` (AST), `_strip_comments_from_source` (shebang-preserving), `_extract()` (orchestrates blocks `CONST|CLASS|FUNC|MISC|MAIN`, guards, `__all__`, TYPE_CHECKING, conditional warnings).

Handles BOM, CRLF warning, non-UTF8 decode, `allow_syntax_errors` partial parse.

## `src/refactor_utils/models.py`

Picklable dataclasses for worker communication:

- `CONST`, `CLASS`, `FUNC`, `MISC`, `MAIN`
- `ImportSpec(module, name, alias, is_from, star, relative)` + `local_name`
- `Occ(start, end, name, kind, eager, obj, specs)` (attr chain)
- `Block(module, kind, names, source, occs, order, bid, orig_path, start_line, end_line, stripped_docstring)` + `merge()`
- `GuardBlock(text, specs)`
- `ModuleInfo(module, path, is_pkg, imports, blocks, guards, declared_all, future_annotations, warnings, errors)`
- `SourceFile(path, module, is_pkg)`
- `DataFile(path, relative, kind)`
- `ConsolidationOptions(keep_docstrings, strip_comments, ..., rename_strategy, ...)` — bundles options for workers.

## `src/refactor_utils/resolver.py`

Cross-module `Resolver(modules, keep_unused_imports, respect_all, entry, workers, rename_strategy, dedupe, imports_mode, all_mode, namespace, preserve_modules, source_map, entry_keep_all, main_name, generate_guard)`.

- `_prefix`/`_suffix`/`_module_path_prefix`/`_sanitize`.
- `_spec_target`, `_resolve_symbol`, `_resolve_dotted` — internal vs external, star expansion.
- `build()` → `_build_envs`, `_analyse` (eager/proj/ext_keys), `_dedupe` (safe/aggressive/none), emitting `emit_keys` via `imports_mode`, `_assign_names` (collision → rename_strategy/error, `main_name` override), `_resolve_ext_conflicts` (import alias), `_render_all`, `_assemble` (topo_order, tail mains, generate_guard, guards), `_exports` (`all_mode`).
- `Plan(future_names, imports_text, guards, blocks, exports, renamed, deduped, warnings)`, `RenderedBlock(section, kind, text, origin, module)`.

## `src/refactor_utils/ordering.py`

`topo_order(nodes, deps, rank, key)` — heap-based topological sort grouping by `rank` (CONST/MISC < CLASS < FUNC), cycle detection with warning `Circular import-time dependency: emitting X before Y`.

## `src/refactor_utils/writer.py`

- `_sep` (spacing), `_format_with_black`/`_format_with_ruff`.
- `render_consolidated(plan, source_name, namespace, preserve_modules, source_map, do_format, formatter)` — section headers `Constants/Classes/Functions/Entry points`, per-block prefix comments (`# Original module:`, `# source:`, `# namespace:`), formatting.
- `render_init(plan, source_name, module)` — `from ._consolidated import (...)` + `__all__`.
- `render_flat`, `render_split` (dict `const.py`/`class.py`/etc.), `render_package`, `render_module`.

## `src/refactor_utils/imports.py`

- `render_import_line(ImportSpec)`, `_group` (stdlib vs third-party via `sys.stdlib_module_names`), `_item_key` (constant/upper/lower sorting), `_wrap` (88-col), `future_section`, `build_import_section(keys, ext_final)` — groups `plain` (import) and `froms`, handles `star`, sorting, wrapping.

## `src/refactor_utils/logging_setup.py`

`setup_logging(level, log_file)` — `loguru` remove/add stderr with green/time format, optional file sink `DEBUG` 5MB rotate.

## `src/refactor_utils/ordering.py`

(see above) + uses `loguru`.

## `src/refactor_utils/data/`

Example data files demonstrating preservation:

- `sample.json` — `{"project": "refactor_utils", ...}`
- `example.csv` — `id,name,value`
- `config.ini` — `[pyrefactor]` example
- `README.md` — explains handling (see `docs/data-files.md`).

## `src/refactor_utils.egg-info/` & `src/refactor_utils/iwgfyrkugf.py`

- Egg-info generated by `setuptools` (PKG-INFO, SOURCES.txt, entry_points).
- `iwgfyrkugf.py` — vendored previous consolidation example, excluded via `EXCLUDED_FILE_GLOBS` to avoid self-loop.

## Top-Level

- `prompt.md` — original AI agent task (features/edge cases to implement).
- `mkdocs.yml` — Material theme, nav, plugins (`search`, `mkdocstrings`).
- `docs/` — see [Architecture](architecture.md).
- `tests/` — pytest suite (discovery, parser, resolver, writer, cli, core, data, integration).
- `examples/` — runnable demos (basic, advanced, programmatic, data_files_example, config_example).
- `.gitignore` — ignores `output/`, `output_*/`, `.venv/`, `__pycache__/`, etc.
