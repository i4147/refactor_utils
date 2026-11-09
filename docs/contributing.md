# Contributing

## Workflow

```bash
git clone ...; pip install -e ".[test,docs]"
pyrefactor src/refactor_utils -o /tmp/out --validate --strict
pytest -q
mkdocs serve
```

## Adding a Flag

1. `cli.py` `build_parser()` — add `add_argument` with help mentioning module.
2. `core.consolidate()` — add param, merge `pyproject.toml` if needed, pass to resolver/parser/writer.
3. `discovery`/`parser`/`resolver`/`writer` — implement_effect.
4. `models.py` — if worker data needed, add field (keep picklable).
5. README + `docs/cli-reference.md` flag table + `docs/edge-cases.md` if needed.
6. `tests/test_cli.py` + `tests/test_core.py` + `examples/`.

## Testing

```bash
pytest
pytest --cov=refactor_utils
pytest -m "not slow"
```

## Docs

```bash
mkdocs serve      # http://127.0.0.1:8000
mkdocs build      # site/
```

Style: Google docstrings (mkdocstrings), Material theme.
