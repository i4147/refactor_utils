# pyrefactor

Consolidate a python module / package / project into a **single, dependency-ordered
file** that works as a drop-in replacement for the original package, with optional
**data-file embedding** so the result is fully self-contained.

```bash
pyrefactor src/my_pkg -o dist            # consolidated dist/my_pkg.py + copied data files
pyrefactor src/my_pkg -o dist --embed-data --no-copy-data   # everything in one file
pyrefactor src/my_pkg --stdout > single.py
pyrefactor src/my_pkg -o dist --check    # CI mode: fail if the output would change
```

## What it does

1. **Discovers** python sources (package layout, `src/` layout, or a single file)
   plus data files (json/yaml/ini/csv/... and extension-less names like `LICENSE`).
2. **Parses** every module with tree-sitter, splitting it into blocks
   (constants, classes, functions, `__main__` guards, conditional import guards)
   and recording every name reference with full scope awareness
   (parameters, loop/with/except/walrus/match bindings shadow module names).
3. **Resolves** internal imports: `from .core import Person` becomes a direct
   reference to the definition. External imports are pruned to only what is
   actually used (`--imports keep|prune|strict`), and name collisions between
   modules are auto-renamed consistently at definition *and* every use site.
4. **Orders** blocks topologically so module-level (import-time) dependencies
   come first; entry points and `__main__` guards are merged at the bottom.
5. **Renders** one valid python file (validated with `ast.parse` before writing)
   and copies or embeds the data files.

## Data files: copy vs embed

By default data files are **copied** next to the consolidated file
(`--no-copy-data` to disable), preserving relative paths.

`--embed-data` additionally (or instead) embeds them **inside** the consolidated
file:

* **text data files** (json, ini, yaml, toml, csv, txt, xml, ...) are embedded as
  **plain text** string literals — still human-readable in the generated file;
* **non-text data files** (fonts, sqlite, pickle, images, ...) are embedded as
  **base64** string literals and decoded on access.

The generated module then exposes:

```python
get_resource("data/settings.json")  # -> str (text) or bytes (binary)
get_resource("data/settings.json", encoding="utf-8")  # force str/bytes conversion
list_resources()  # sorted names of embedded resources
with resource_path("assets/logo.ttf") as path:  # real temp-file path
    font = ImageFont.truetype(path, 24)
```

Source code that should work both as a package and consolidated can use the
accessor if present and fall back to a file path otherwise:

```python
def load_settings():
    try:
        import json

        return json.loads(get_resource("data/settings.json"))  # consolidated file
    except NameError:
        return json.loads(
            (Path(__file__).parent / "data" / "settings.json").read_text()
        )
```

See `examples/demo_project` and `examples/run_demo.py` for a complete, runnable
demo (including a "fake font" binary resource).

## Library API

```python
from pyrefactor import consolidate, validate_syntax, ConsolidationResult

result = consolidate(
    "src/my_pkg",
    "dist",
    embed_data=True,
    copy_data=False,
)
result.files  # [dist/my_pkg.py]
result.exports  # number of exported names
result.embedded_data_files  # resource names embedded in the file
result.plan.blocks  # rendered blocks with origin comments
```

Useful flags: `--entry MODULE` (keep only one `__main__` guard), `--main-name`
(rename the entry function), `--generate-guard`, `--keep-docstrings=false`,
`--keep-comments=false`, `--diff`, `--dry-run`, `--check`, `--format`
(black/ruff), `-j/--workers`, `--executor process|thread`, `--progress`
(needs tqdm).

Configuration can come from a `pyproject.toml` next to the source:

```toml
[tool.pyrefactor]
exclude = ["experimental/*"]
imports = "prune"
copy_data = true
embed_data = false
data_glob = ["*.json"]
```

## Dependencies

`libcst` (lossless concrete-syntax-tree parsing/rewriting). Optional: `tqdm` (progress),
`black`/`ruff` (`--format`). Logging uses the standard library only.

## Development

```bash
pip install -e .[dev]
pytest
python examples/run_demo.py
```

See `CHANGES.md` for the list of fixes and improvements in this revision.
