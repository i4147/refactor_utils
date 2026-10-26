# Data Files

This directory contains example data files that demonstrate proper handling of project data during consolidation.

When `pyrefactor` processes a project, it automatically discovers and preserves data files alongside Python code:

- **Discovered via**: `discovery.py` → `discover_data_files()`
- **Copied to**: output directory preserving relative paths
- **Controlled by**: `--copy-data` / `--no-copy-data` / `--data-glob`

## Files

- `sample.json` - Example JSON data (common for configs)
- `example.csv` - Example CSV data (common for datasets)
- `config.ini` - Example INI config

These files are automatically copied when running:

```bash
pyrefactor src/refactor_utils -o output --copy-data
# or
pyrefactor src/refactor_utils -o output --data-glob "*.json"
```

See main README.md for full data handling documentation.
