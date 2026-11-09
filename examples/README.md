# Examples

This directory demonstrates various usage patterns for `pyrefactor` (`refactor_utils`).

## Contents

- **basic_usage.py** — Minimal consolidation via CLI and Python API.
- **advanced_flags.py** — Showcasing `--rename-strategy`, `--dedupe`, `--all`, `--source-map`, etc.
- **programmatic_api.py** — Using `refactor_utils.consolidate()` directly.
- **data_files_example/** — Project with data files that are preserved.
- **config_example/** — `pyproject.toml` configuration.

Run all examples from repo root:

```bash
pip install -e .
python examples/basic_usage.py
python examples/advanced_flags.py
python examples/programmatic_api.py
```

## See Also

- `docs/` — Full documentation (mkdocs)
- `tests/` — Pytest suite with integration examples
