# autoall

Auto-generate `__all__` for any Python package, on first import, once.

## Install

```bash
pip install autoall
# or
uv add autoall
```

## Use it

### A. Let the CLI patch a package

```bash
autoall install path/to/mypkg       # dir, __init__.py, or dotted name
autoall status  mypkg
autoall uninstall mypkg
autoall regen   mypkg               # force re-scan of an imported package
```

### B. Patch by hand (2 lines at the bottom of `__init__.py`)

```python
# --- autoall: ensure __all__ ---
from autoall import ensure_all as _ensure_all

_ensure_all(__package__ or __name__)
# --- end autoall ---
```

### C. Programmatic

```python
from autoall import install

install("mypkg")
```

## What happens on first import

1. `ensure_all` runs at the bottom of `__init__.py`.
2. No `__all__` yet, no marker block → scan directory, import each public
   `.py` / subpackage (a genuinely missing dependency **re-raises**).
3. `__all__` is set on the module **and** written once to `__init__.py`:

   ```python
   # --- auto-generated __all__ (delete to regenerate) ---
   __all__ = [
       "core",
       "filter",
       "gradient",
       "linking",
       "script",
       "shape",
       "structure",
       "style",
       "text",
   ]
   # --- end auto-generated __all__ ---
   ```

On every subsequent import: block is parsed with `ast.literal_eval`, no I/O.

## Priority rules

| Situation                                       | Behaviour                    |
| ----------------------------------------------- | ---------------------------- |
| User `__all__` outside the block                | **Respected**                |
| Auto-block present and parseable                | Used as-is, no writes        |
| Auto-block present but broken                   | Regenerated                  |
| Nothing defined                                 | Scan + write once            |
| Submodule missing                               | Silently skipped             |
| Submodule's *dependency* missing                | Re-raised                    |
| `__init__.py` read-only                         | In-memory `__all__` + warn   |
| Namespace package (`__file__` is None)          | Warned, skipped              |