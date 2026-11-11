# refactor_utils

`refactor_utils` consolidates a small or medium Python module, library or
project into **one** dependency-ordered file. It installs the `pyrefactor`
command.

```bash
pip install -e .
pyrefactor path/to/package        # writes ./output/<current-directory-name>.py
```

A run without flags produces exactly one generated Python file:

```text
$ cd myproject
$ pyrefactor src/mypackage
output/
  myproject.py        # imports -> constants -> classes -> functions -> entry points
```

## What it does

| Step | Behaviour |
|------|-----------|
| Discover | Walks the source tree, skipping tests, virtualenvs, build output and a few well-known files. Data files (`.json`, `.csv`, ...) are copied next to the result. |
| Parse | Every file is parsed with tree-sitter in parallel workers and split into top-level blocks. |
| Resolve | Same-named objects are deduplicated by content hash or renamed to a unique, **valid Python identifier**. Every reference is rewritten. |
| Order | Blocks are topologically sorted by import-time dependencies: constants, classes, functions, entry points. |
| Render | Imports are merged, pruned and sorted. Module docstrings and `__all__` are dropped. `__main__` guards are merged into one. |
| Validate | The result is parsed with `ast`. Code that is not valid Python is never written. |

## Where to go next

- New here: [Getting started](getting-started.md)
- All flags: [Command line](usage/cli.md)
- Using it from Python: [Python API](usage/python-api.md)
- Why a name was renamed: [Name resolution and renaming](internals/name-resolution.md)
- Something went wrong: [Troubleshooting](troubleshooting.md)

## Requirements

- Python 3.10 or newer
- `tree-sitter`, `tree_sitter_python`, `loguru` (installed automatically)
- optional: `black` or `ruff` for `--format`, `tqdm` for `--progress`

## License

MIT. Repository: `i4147/refactor_utils`.
