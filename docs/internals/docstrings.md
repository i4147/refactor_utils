# Docstrings, comments and __all__

## Module docstrings are pruned

The docstring of a source module describes a file that no longer exists in
the result. It is never written to the consolidated file. The result has
exactly one module docstring, the generated one:

```python
"""Consolidated from ``mypackage`` by pyrefactor (generated file)."""
```

Pruned:

| Source | Why |
|--------|-----|
| a string as first statement, also after a shebang, an encoding line or comments | module docstring |
| implicitly concatenated strings, raw strings, single or triple quotes | same thing, different spelling |
| a bare string after `from __future__ import ...` or anywhere else at module level | no runtime effect; it would otherwise be emitted as a constant |
| a bare string directly inside `if TYPE_CHECKING:` | same |

Kept:

- docstrings of classes, functions and methods (unless `--no-keep-docstrings`);
- f-string statements such as `f"{setup()}"`: their expressions are evaluated,
  so they are code, not documentation;
- strings assigned to a name: `__doc__ = "..."`, `USAGE = """..."""`.

!!! note "Changed behaviour"
    Earlier versions only skipped a docstring when it was a single plain
    string in first position. Concatenated docstrings and later bare strings
    were emitted in the Constants section.

## Class and function docstrings

`--no-keep-docstrings` removes the docstrings of the emitted blocks.
`--keep-docstrings` (the default) keeps them.

## Comments

Comment lines directly above a block travel with that block, as does a
trailing comment on its last line. `--no-keep-comments` strips comments.
Comments attached to a pruned docstring are dropped with it.

## `__all__`

`__all__` is never emitted as code. `__all__ = [...]`, `__all__ += [...]`,
`__all__.append(...)` and annotated forms are read as hints (they define what
an internal star import expands to) and then dropped. The exported surface
reported in the log and in `ConsolidationResult.exports` is recomputed from
the public names that survive.
