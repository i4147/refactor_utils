# Project Files

Every file in `src/refactor_utils/` and top-level.

## `pyproject.toml`

- `[build-system]` setuptools.
- `[project]` name `refactor_utils`, version `1.5.0`, deps `loguru`, `tree-sitter`, `tree_sitter_python`; optional `[format]`, `[progress]`, `[test]`, `[docs]`, entry point `pyrefactor = refactor_utils.cli:main`.
- `[tool.setuptools]` where=`src`, package-data `data/*`.
- `[tool.pyrefactor]` default config for this repo (exclude `tests/*`, `iwgfyrkugf.py`).
- `[tool.pytest]` `testpaths = ["tests"]`, `pythonpath = ["src"]`.
- `[tool.black]`/`[tool.ruff]`/`[tool.coverage]`/`[tool.mkdocstrings]`.

## `src/refactor_utils/__init__.py`

Public API: re-exports `ConsolidationError`, `ConsolidationResult`, `consolidate` and `consolidate_package` from `core.py`, plus `__version__` (1.5.0).

## `src/refactor_utils/cli.py`

`build_parser()` defines all flags with help referencing the module that
implements them. `apply_config(args, parser)` merges `[tool.pyrefactor]`
(explicit `--config` or auto-detected via
`discovery.detect_pyproject_config()`); a flag that differs from its argparse
default is treated as explicit and wins. `main()` resolves aliases
(`--jobs`→`--workers`, `--progress-bar`, `--keep-all-mains`,
`--post-format`), calls `consolidate_package()`, and maps failures to exit
codes (`ConsolidationError` → 2, unexpected → 1). `_read_tool_config(path)`
parses an explicit TOML file.

## `src/refactor_utils/core.py`

Orchestrates `discover → _parse_all (parallel) → Resolver → writer`.

- `_parse_worker` (picklable), `_parse_all` (Process/Thread pool with
  BrokenExecutor fallback, tqdm).
- `ConsolidationResult` (fields: `output_dir`, `files`, `modules`, `skipped`,
  `exports`, `warnings`, `diff`, `stdout_text`, `copied_data_files`,
  `validated`, `dry_run`, `plan`, `all_added`, `pkg_name`).
- `consolidate_package(source=".", output=None, **options)` — the
  `pyrefactor [pkg]` contract (see [Quickstart](quickstart.md)): package-name
  detection, `consolidated/<pkgname>.py` output, temporary
  `const.py`/`classes.py`/`func.py` extraction (`_write_section_files`,
  `_merge_body`), `__all__` added in place by `autoall` (`_render_final`),
  stale-output cleanup (`_cleanup_stale_outputs`), `--diff` (`_unified_diff`)
  and optional formatting (`writer.format_source`).
- `consolidate(...)` — the lower-level multi-layout API
  (`mode="single"|"package"|"module"|"flat"|"split"`) kept for library users.

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
- `normalize_pkg_name(name)` — turns a distribution/folder name into a valid
  module name (`my-dist` → `my_dist`, leading digit prefixed with `_`).
- `detect_pkg_name(root)` — reads `pyproject.toml`/`setup.cfg`/`setup.py` in
  `root` and up to two levels above (covering `src/<pkgname>`), falling back
  to the folder name; always returns a normalised identifier.
- `discover_data_files(..., data_glob=())` — optional glob filter matched
  against the file name and its relative path.

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
- `build()` → `_build_envs`, `_analyse` (eager/proj/ext_keys), `_dedupe` (safe/aggressive/none), `_emit_keys` via `imports_mode`, `_assign_names` (collision → rename_strategy/error, `main_name` override), `_resolve_ext_conflicts` (import alias), `_dedupe_post_rename` (`_block_signature`, fixed point), `_render_all`, `_assemble` (topo_order, tail mains, generate_guard, guards), `_exports` (`all_mode`).
- `Plan(future_names, imports_text, guards, blocks, exports, renamed, deduped, warnings)`, `RenderedBlock(section, kind, text, origin, module)`.

## `src/refactor_utils/ordering.py`

`topo_order(nodes, deps, rank, key)` — heap-based topological sort grouping by `rank` (CONST/MISC < CLASS < FUNC), cycle detection with warning `Circular import-time dependency: emitting X before Y`.

## `src/refactor_utils/writer.py`

- `_sep` (spacing), `_format_with_black`/`_format_with_ruff`,
  `format_source(text, formatter)` (public wrapper used by
  `consolidate_package(--format)`).
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
- `docs/` — see [Architecture](architecture.md); `docs/README.md` explains how to build the HTML docs and the single-file PDF.
- `tests/` — pytest suite (discovery, parser, resolver, writer, cli, core, data,
  integration). `tests/test_consolidated.py` covers the
  `consolidated/<pkgname>.py` flow, `tests/test_cli_workflow.py` drives the CLI
  end to end (no-arg run, `--check`, config merge, exit codes) and
  `tests/test_autoall.py` pins the in-place `__all__` behaviour.
- `examples/` — runnable demos (basic, advanced, programmatic, data_files_example, config_example).
- `arena/` — helper scripts (bash/python) used during this session; see
  `arena/README.md`. Includes the docstring audit, the PDF builder and the
  repo-wide consolidation demos.
- `refactor_utils.pdf` — generated single-file documentation PDF (see
  `docs/README.md`).
- `site/` — generated HTML documentation (git-ignored).
- `.gitignore` — ignores `output/`, `output_*/`, `.venv/`, `__pycache__/`, etc.

## `src/refactor_utils/discovery.py` (additions)

- `detect_pkg_name(root)` — detects the consolidated package name by reading
  `pyproject.toml` (`[project] name`), `setup.cfg` (`[metadata] name`) or
  `setup.py` (`setup(name=...)`) in the package folder and up to two directory
  levels above it (covering `src/<pkgname>` layouts); falls back to the
  folder's own name.

## `src/refactor_utils/core.py` (additions)

- `consolidate_package(source, output=None, **kwargs)` — implements the
  `pyrefactor [pkg]` contract: detects the package name, creates a
  `consolidated` output directory, runs the existing
  `discover → parse → resolve` pipeline, writes temporary `const.py` /
  `classes.py` / `func.py` section files, assembles them (plus the optimized,
  local-imports-stripped import section and entry points) into a single
  `consolidated/<pkgname>.py`, removes the temporaries, and delegates
  `__all__` generation to `autoall.ensure_all_file`. Returns a
  `ConsolidationResult` extended with `all_added` and `pkg_name`.

## `src/refactor_utils/writer.py` (additions)

- `render_consolidated_single(plan, source_name)` — renders one module file:
  `__future__` → optimized imports → `TYPE_CHECKING` guard → constants →
  classes → functions → entry points, with per-block namespace/source-map
  comments when requested.

## `autoall/src/autoall/__init__.py` (additions)

- `ensure_all_file(path, *, persist=True)` — scans a single `.py` module file
  (no import required) for public top-level names (functions, classes,
  constants) and writes/updates an `__all__ = [...]` block between the
  `# --- auto-generated __all__ ---` markers inplace (atomically). Exported
  as `autoall.ensure_all_file`.

## `autoall/` (vendored helper package)

`autoall` is developed as a sibling package in `autoall/` and used by
`pyrefactor` to write the `__all__` section:

- `ensure_all(pkg_name, ...)` — package mode: parse the user part of
  `__init__.py`, respect a user `__all__`, otherwise scan the directory and
  write the block once.
- `ensure_all_file(path, *, persist=True)` — **file mode used by
  `pyrefactor`**: parse a single module with `ast`, collect public top-level
  names and write/update the marker-delimited `__all__` block in place
  (atomic replace, idempotent, CRLF preserving, user `__all__` respected).
- `build_all_block(names)` — public helper returning the exact block text, so
  `--check`/`--stdout` can show what will be written.
- `install`/`uninstall`/`is_installed`/`regenerate` and the `autoall` CLI.

## `arena/` (session helper scripts)

| Script | Purpose |
|--------|---------|
| `setup.sh` | create the venv, install the project + test/docs deps |
| `run_tests.sh` | run pytest (optionally with coverage) |
| `check_all.sh` | tests + docstring audit + docs + PDF + demos |
| `demo_consolidate.sh` | run `pyrefactor` over the example packages |
| `test_on_packages.sh` | run `pyrefactor` over real packages (autoall, this repo, examples) and compile the result |
| `verify_prompt.py` | assert the `prompt.md` contract (layout, temp files, `__all__`, ordering) |
| `helpers.py` | small python helpers (make demo packages, validate output) |
| `docstring_audit.py` | report missing module/class/function docstrings |
| `add_docstrings.py` | libcst script that inserted the missing docstrings |
| `build_docs.sh` | build the mkdocs HTML site |
| `build_pdf.py` | build `refactor_utils.pdf` (fpdf2, no browser needed) |

## Documentation build

`docs/README.md` documents the two-step build (HTML site + single-file PDF),
including the tools required and how to verify the result.
