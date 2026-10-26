# Output Modes

`--mode` controls layout; `single` (default) is backward-compatible.

## `single` (default)

```
output/
  __init__.py        # from ._consolidated import ... ; __all__ = [...]
  _consolidated.py   # docstring, future, imports, guards, constants, classes, functions, entry points
```

`core.py` calls `writer.render_consolidated()` + `render_init()`.

## `package`

```
output/<pkg>/
  __init__.py
  _consolidated.py
  (data preserved under output/<pkg>/...)
```

Uses `render_package()`; package name derived from source dirname/stem.

## `module`

Single file, no `__init__.py`:

```
output/_consolidated.py  # or output (if path ends with .py, that file)
```

`render_module()`; `--stdout` similar.

## `flat`

No section headers, minimal spacing:

```python
"""Flat consolidated from ``pkg``."""
from __future__ ...
import os
X = 1
class Bar: ...
```

`render_flat()`; still includes `__init__.py` for compatibility.

## `split`

By kind:

```
output/
  const.py   # constants + future + imports
  class.py   # classes
  func.py    # functions
  main.py    # entry points
  __init__.py
```

`render_split()` returns dict `fname → content`.

## Choosing a Mode

- **Library** for publishing: `single` (standard).
- **Package** installable: `package`.
- **Script**: `module` + `--stdout`.
- **Debugging**: `flat`.
- **Large project** split by kind: `split`.

All modes compile (`compile(..., "exec")`) and copy data files under the output root (or package dir).
