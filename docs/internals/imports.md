# Imports

## Internal imports disappear

An import of a module that is part of the consolidation (relative, or absolute
below a known top-level package) is resolved to the definition it points to
and removed. References are rewritten to the final name.

An internal import that cannot be resolved (the module was excluded or does
not exist) is reported:

```text
WARNING | 1: cannot resolve internal import 'tokenizers.Tokenizer' (module skipped, missing or excluded?)
```

## External imports are merged

External imports from all modules are collected into one section:

- `from __future__` imports first, merged into a single line;
- standard library, then everything else, separated by a blank line;
- inside a group: `import x` lines, then `from x import ...` lines, sorted
  case-insensitively by module;
- imported names starting with `_` sort before the others;
- lines longer than 88 columns are wrapped in parentheses, one name per line.

## Modes

| `--imports` | Behaviour |
|-------------|-----------|
| `prune` (default) | only imports used by a surviving block are emitted |
| `keep` | every external import is emitted |
| `strict` | like `prune`, plus a warning for each dropped import |

## Conflicts

When two different imports bind the same local name, one keeps the name and
the others get an alias built from the module path; the references in the
affected modules are rewritten. Aliases are valid identifiers, see
[Name resolution and renaming](name-resolution.md).

## Guarded imports

```python
try:
    import ujson as json
except ImportError:
    import json
```

`try/except ImportError` and `if sys.platform ...` blocks that only contain
imports are kept verbatim after the import section. Imports they already
perform are not hoisted a second time, and names they bind are never reused
for a renamed definition.

## TYPE_CHECKING

The body of `if TYPE_CHECKING:` is flattened: its imports join the normal
import section, and constants, classes and functions defined there become
ordinary blocks.

## Nested imports

An import inside a function or class stays where it is. If it refers to an
internal module it is removed and the references are rewritten.

## Star imports

- internal: expanded to the names the module exports (its `__all__` when
  declared, otherwise the public names);
- external: kept as `from x import *`, with a warning.
