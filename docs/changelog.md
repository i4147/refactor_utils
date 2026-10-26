# Changelog

## 1.4.8 — Prompt.md Full + Tests/Examples/Docs

- Implements all `prompt.md` flags (dry-run, stdout, config, format, diff, overwrite, include, docstrings/comments, rename-strategy, dedupe, imports, all, entry, namespace, source-map, fail-on-warning, max-skipped, allow-syntax-errors, jobs, validate, modes, pyproject awareness).
- Edge-case hardening (dynamic, __all__, star, conditional, circular, scoping, runtime, discovery/IO).
- Data-file preservation (`discover_data_files` → copy).
- Added `tests/` (pytest 50+ cases), `examples/` (basic/advanced/programmatic/data/config), `docs/` (mkdocs Material + mkdocstrings), documented every project file.

## 1.4.7

- Initial AI-generated `pyrefactor` (`single` mode, tree-sitter, parallel, topo ordering, collision rename prefix).

## 1.4.8-pre

- Added `pyproject.toml` `[tool.pyrefactor]` example, `src/refactor_utils/data/` examples.
