# Getting started

## Install

```bash
git clone https://github.com/i4147/refactor_utils
cd refactor_utils
python -m venv .venv
.venv/bin/pip install -e .
```

Optional extras:

| Extra | Installs | Needed for |
|-------|----------|------------|
| `.[format]` | black | `--format` |
| `.[progress]` | tqdm | `--progress` |
| `.[dev]` | pytest, ruff | running the test suite |
| `.[docs]` | mkdocs, mkdocs-material, xhtml2pdf | building this documentation and `docs.pdf` |

## First run

Given this project:

```text
shop/
  cart.py        # class Cart, def total()
  pricing.py     # TAX = 0.2, def total()   <- different body than cart.total
  __init__.py
```

run:

```bash
cd shop
pyrefactor .
```

`output/shop.py` is created. The file name is the name of the directory
`pyrefactor` was started in; override it with `--name`.

The result has a fixed layout:

```python
"""Consolidated from ``shop`` by pyrefactor (generated file)."""

import json                      # merged, pruned, sorted imports

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TAX = 0.2

# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

class Cart: ...

# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

def total(): ...                 # from cart (first module alphabetically)

def pricing_total(): ...         # renamed: same name, different body
```

A section header is only written when the section has content. A block that
depends on a later section (a constant computed by calling a function, for
example) is moved down so the file still runs top to bottom.

## Preview before writing

```bash
pyrefactor . --dry-run        # log the plan, write nothing
pyrefactor . --stdout         # print the module instead of writing it
pyrefactor . --diff           # unified diff against the existing output
```

## What gets skipped

Skipped by default:

- directories: `tests`, `test`, `testing`, `docs`, `doc`, `.git`, `.venv`,
  `venv`, `env`, `build`, `dist`, `node_modules`, caches, `site-packages`,
  IDE folders, and the output directory itself;
- files: `__main__.py`, `conftest.py`, `test_*.py`, `*_test.py`, `setup.py`,
  `iwgfyrkugf.py`, `_consolidated.py`.

Bring something back with `--include GLOB`, skip more with `-x GLOB`.
Precedence is: default excludes < `--include` < `--exclude`.

!!! warning "Do not keep an old result inside the source tree"
    A previously generated file that lives inside the source directory (and
    not in the output directory) is treated as source on the next run. Keep
    results in `output/` or exclude them with `-x`.
