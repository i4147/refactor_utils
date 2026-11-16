# Data Files

This directory contains example data files that demonstrate proper handling of project data during consolidation.

When `pyrefactor` processes a project, it discovers data files alongside Python code and can either copy them to the output directory, embed them in the generated `.py` file, or both.

## Discovery

- Discovered via `discovery.py` → `discover_data_files()`
- Controlled by `--copy-data` / `--no-copy-data` / `--data-glob`

## Copying

When `--copy-data` is enabled (default) files are written to the output directory preserving their relative paths.

```bash
pyrefactor src/refactor_utils -o output --copy-data