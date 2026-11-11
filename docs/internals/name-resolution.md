# Name resolution and renaming

All modules end up in one namespace, so two modules that define the same name
collide. The resolver handles every collision in one of two ways.

## Identical bodies are merged

Definitions with the same kind, the same name and the same content hash are
emitted once. The hash ignores blank lines and trailing whitespace. The copy
from the shallowest module (then alphabetical order) is kept.

```text
INFO | Dropping duplicate func 'slugify' from pkg.utils.text (identical to pkg.text)
```

## Different bodies are renamed

When the bodies differ, both definitions are kept:

- the first module keeps the plain name. Modules are ordered by depth (fewest
  dots first), then alphabetically;
- every other definition becomes `<module_prefix>_<name>`.

The module prefix is the dotted module path with dots replaced by `_`. When
the project has a single top-level package, that package name is left out:

| Module | Name | Result |
|--------|------|--------|
| `pkg.cart` | `total` | `total` |
| `pkg.pricing` | `total` | `pricing_total` |
| `pkg.io.json` | `load` | `io_json_load` |
| `pkg.pricing` | `_cache` | `_pricing_cache` (leading underscores stay in front) |
| `pkg.pricing` | `__version__` | `__pricing_version__` |

If the candidate is already taken, `_2`, `_3`, ... is appended.

A name is also renamed when it would shadow an external import that another
module uses under the same local name, or a name bound by an import guard.

## Generated names are always valid Python

File names are not required to be identifiers. `2.py`, `my-mod.py` or
`class.py` are perfectly good file names, but `2_Tokenizer`, `my-mod_run` and
a bare `class` are not names Python accepts.

Every generated name therefore goes through `make_identifier()`:

| Rule | Example |
|------|---------|
| characters that cannot be part of an identifier become `_` | `my-mod` -> `my_mod` |
| a name that would start with a digit gets an `m` prefix | `2` -> `m2` |
| a Python keyword gets a trailing `_` | `class` -> `class_` |
| an empty result falls back to `mod` | `---` -> `mod` |

The `m` prefix (rather than `_`) keeps the renamed object public, so it still
counts as an exported name.

### Example

`1.py` and `2.py` both define `Tokenizer`, `Encoding` and `__version__` with
different bodies:

```text
INFO | Renamed 2:Encoding -> m2_Encoding
INFO | Renamed 2:Tokenizer -> m2_Tokenizer
INFO | Renamed 2:__version__ -> __m2_version__
```

```python
class Tokenizer: ...           # from 1.py

class m2_Encoding: ...         # from 2.py

class m2_Tokenizer:            # from 2.py
    def encode(self, text):
        return m2_Encoding(...)    # reference rewritten
```

Before this rule existed the second class was written as
`class 2_Tokenizer:`, which is a syntax error.

The same guarantee applies to:

- aliases created for conflicting external imports
  (`from a import load` + `from b import load` -> `from b import load as b_load`);
- names derived from `--main-name`. The value itself is checked up front and
  rejected with exit code 2 when it is not an identifier.

As a last line of defence, the [validation](validation.md) step refuses to
write any file that does not parse.

## References are rewritten

After names are assigned, every recorded occurrence is rewritten:

- plain names: `Tokenizer(...)`;
- imports with aliases: `from .two import Tokenizer as T` then `T(...)`;
- attribute access through a module: `two.Tokenizer(...)`;
- annotations, decorators, base classes and default values.

Strings are not rewritten: `"Tokenizer(...)"` inside a `__repr__` keeps the
original text.

## Scope

Renaming is name-based. Function parameters and class bodies are treated as
scopes; other local variables are not. See [Known limits](../limitations.md).
