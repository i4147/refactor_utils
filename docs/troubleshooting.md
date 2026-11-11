# Troubleshooting

## `N file(s) could not be parsed; refusing to emit a partial consolidation`

A source file has a syntax error. The log names the file and the line:

```text
ERROR | /path/mytest/result.py: syntax error near line 590
```

Fix the file, or exclude it with `-x`. A common cause is an old, broken
generated file that still lives inside the source directory.

## `generated X.py is not valid python`

The syntax check rejected the result, and nothing was written. This is a bug
in `pyrefactor`. See [Validation](internals/validation.md).

## A class was renamed to `m2_Something`

Two modules define `Something` with different bodies, and the second module's
file name starts with a digit (`2.py`). See
[Name resolution and renaming](internals/name-resolution.md).

## `cannot resolve internal import`

A module imports from a sibling that was excluded, skipped or does not exist.
The import is dropped; references to the name are left as they are and may be
undefined at run time. Include the missing module (`--include`) or fix the
import.

## An import I need was removed

`--imports prune` drops imports that no surviving block references by name.
Imports needed only for their side effects are affected. Use `--imports keep`.

## The module docstring is gone

That is by design: see [Docstrings](internals/docstrings.md). To keep the
text, assign it to a name, for example `USAGE = """..."""`.

## Only one `__main__` block is left

Guards are merged into one. Use `--entry MODULE` to select which entry point
survives. See [Entry points](internals/entry-points.md).

## The result parses but fails when imported

Validation is syntax-only. Typical causes:

- the original code relied on `__file__`, `__name__` or `__package__`
  (a warning is logged);
- dynamic imports through `importlib` or `__import__` (a warning is logged);
- the original module itself fails at import time.

## `--name` did not change the class or function names

`--name` is only the output file name. Use `--main-name` to rename `main`.

## More detail

```bash
pyrefactor . --log-level DEBUG --log-file pyrefactor.log
```
