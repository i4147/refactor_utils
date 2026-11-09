# Installation

## Requirements

- Python ≥ 3.10
- `tree-sitter` + `tree_sitter_python`
- `loguru`

## From Source

```bash
git clone https://github.com/i4147/refactor_utils.git
cd refactor_utils
pip install -e .
```

## Extras

```bash
pip install -e ".[format]"   # black + ruff for --format
pip install -e ".[progress]"  # tqdm for --progress
pip install -e ".[test]"      # pytest + coverage
pip install -e ".[docs]"      # mkdocs + material + mkdocstrings
```

Or all:

```bash
pip install loguru tree-sitter tree_sitter_python black ruff tqdm pytest mkdocs mkdocs-material "mkdocstrings[python]"
```

## Verify

```bash
pyrefactor --help
pyrefactor --version
pytest -q
mkdocs serve
```

## Development Setup

```bash
pip install -e ".[test,docs,format,progress]"
pre-commit install  # if configured
```

## Troubleshooting

- **`externally-managed-environment`** on Debian/Ubuntu: `pip install -e . --break-system-packages` or use `pipx`/venv.
- **`ModuleNotFoundError: tree_sitter`**: ensure `tree-sitter` and `tree_sitter_python` are installed for your Python.
- **Black/ruff not found for `--format`**: install via `pip install black ruff`; otherwise formatting is skipped with a warning.
