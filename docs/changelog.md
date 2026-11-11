# Changelog

## Unreleased

### Fixed

- Collision renames could produce names that are not valid Python. With
  modules `1.py` and `2.py` both defining `Tokenizer`, the second class was
  written as `class 2_Tokenizer:`. Generated names now always pass through
  `make_identifier()`: `m2_Tokenizer`. The same applies to module names with
  dashes or other punctuation, to keywords, to aliases generated for
  conflicting imports and to names derived from `--main-name`.
- `--main-name` is rejected with exit code 2 when it is not an identifier.
- The package could not be imported on Python 3.10 to 3.13 because
  `models.py` used a forward reference in an annotation without
  `from __future__ import annotations`.

### Added

- `validate_syntax(text, filename)`: a small `ast`-based validator. It runs on
  every generated module before anything is written. It replaces the old
  compile-and-import validator, which was too strict and had been disabled.
- Documentation under `docs/` (MkDocs), `docs.pdf`, and helper scripts under
  `tools/`.

### Changed

- Module docstrings of source files are pruned in every form (concatenated
  strings, strings after `from __future__`, stray bare strings). Previously
  some of them were emitted in the Constants section.
- `mytest/result.py` was regenerated with the fixed tool.

## 2.0.0

- One layout, one dedupe strategy (content hash), one rename strategy
  (`<module>_<name>`).
- Removed the `--mode`, `--validate*`, `--namespace`, `--rename-strategy` and
  related flags. See [Command line](usage/cli.md#removed-flags).
