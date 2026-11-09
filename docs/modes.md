# Output Layouts

## CLI layout: `consolidated/<pkgname>.py` (only layout of `pyrefactor`)

```bash
pyrefactor src/mypackage
```

```
src/mypackage/
  consolidated/
    mypackage.py     # the single generated module
```

`mypackage.py` contains (in order) the module docstring, `__future__` and
optimized imports, constants, classes, functions, entry points/guards and the
`autoall`-generated `__all__` section. The output directory always holds
exactly one file: temporary `const.py`/`classes.py`/`func.py` extraction files
are removed after the merge, and files from the legacy `output/` layout are
cleaned up when the directory is called `consolidated/` or `output/`.

`core.consolidate_package()` implements this layout;
`writer.render_consolidated_single()` renders the body and
`autoall.ensure_all_file()` appends `__all__` in place.

## Library layouts: `core.consolidate(mode=...)`

The older, lower-level API still supports five layouts. They are not reachable
from the CLI, which keeps `pyrefactor` to a single, predictable output.

### `single` (default of `consolidate()`)

```
output/
  __init__.py        # from ._consolidated import ... ; __all__ = [...]
  _consolidated.py   # docstring, future, imports, guards, constants, classes, functions, entry points
```

`core.py` calls `writer.render_consolidated()` + `render_init()`.

### `package`

```
output/<pkg>/
  __init__.py
  _consolidated.py
  (data preserved under output/<pkg>/...)
```

Uses `render_package()`; package name derived from source dirname/stem.

### `module`

Single file, no `__init__.py`:

```
output/_consolidated.py  # or output (if path ends with .py, that file)
```

`render_module()`; `--stdout` gives the same text on stdout.

### `flat`

No section headers, minimal spacing:

```python
"""Flat consolidated from ``pkg``."""
from __future__ ...
import os
X = 1
class Bar: ...
```

`render_flat()`; still includes `__init__.py` for compatibility.

### `split`

By kind:

```
output/
  const.py   # constants + future + imports
  class.py   # classes
  func.py    # functions
  main.py    # entry points
  __init__.py
```

`render_split()` returns a dict `fname → content`.

## Choosing a layout

- **CLI / publishing a module**: the default `consolidated/<pkgname>.py`.
- **Library users that need the old multi-file tree**: `consolidate(mode=...)`
  with `single`, `package`, `module`, `flat` or `split`.

All layouts compile (`compile(..., "exec")`) and copy data files under the
output root (or package dir).
