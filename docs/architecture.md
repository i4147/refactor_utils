# Architecture

```
cli.py → core.py orchestrates
        → discovery.py (SourceFile, DataFile, pyproject)
        → parser.py (tree-sitter, parallel, picklable ModuleInfo)
        → resolver.py (rename/dedupe/imports/__all__, env/eager)
        → ordering.py (topo sort)
        → writer.py (mode renderers)
        → imports.py (import section)
        → logging_setup.py (loguru)
models.py — picklable dataclasses
```

## Flow

1. **CLI** `build_parser()` parses flags, resolves aliases, calls `consolidate()`.
2. **Discovery** `discover()` walks `detect_base(root)` (`src/` or `.`), filtering `EXCLUDED_*` with `include` precedence, skipping `output`, handling single-file/symlink/namespace.
3. **Data** `discover_data_files()` collects non-py data.
4. **Parse** `_parse_all()` parallel (ProcessPool picklable worker `_parse_worker` + BrokenExecutor fallback to Thread, tqdm optional) → `dict[module, ModuleInfo]`.
5. **Resolver** builds `env` (def + import table + star), analyses `eager`/`proj_refs`/`ext_keys`, dedupes (`none|safe|aggressive`), assigns names (`prefix|suffix|module_path|error`, `main_name`), resolves import conflicts, dedupes again with post-rename identities (`_dedupe_post_rename` → `_block_signature`, fixed point), renders via `_render_block` (replacing `Occ`s), assembles `Plan` (topo_order by rank, tail `main`, guards, exports via `all_mode`).
6. **Writer** renders per `mode` (single/package/module/flat/split) with optional `source_map`/`preserve_modules`/`namespace`/`format`, always `compile()` checked.
7. **Core** handles `dry_run`/`check`/`stdout`/`diff`/`overwrite`/`validate`/`copy_data`, copies data via `shutil.copy2`, returns `ConsolidationResult`.

## Decoupling

Parser only emits `models.py` types (never tree-sitter nodes); resolver consumes them. Workers never import `loguru` heavy state beyond warnings list.

## File Map

See [Project Files](project-files.md) for per-file docs.

## See Also

- `tests/test_integration.py` — end-to-end large/circular/src-layout cases.
- `ordering.topo_order` — cycle handling.
