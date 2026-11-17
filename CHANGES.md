# Changes in 3.0.0 — rewritten on libcst

The whole parsing/rewriting layer now runs on
[libcst](https://github.com/Instagram/LibCST) instead of tree-sitter. The CLI
surface, library API, consolidation semantics and the data-embedding feature
are unchanged; only the dependency (`libcst`, no more `tree-sitter`) and the
internal pipeline changed.

## What the rewrite buys

1. **No more byte-offset bookkeeping.** Blocks keep their libcst statement
   nodes; renames, nested-import replacement and docstring/comment stripping
   are node-level CST transformations. The 2.x class of bugs where stripped
   docstrings invalidated occurrence offsets cannot happen by construction.
2. **Lossless comments and whitespace.** Comments attached to a statement
   (libcst `leading_lines`) travel with their block when it is reordered, so
   consolidated output keeps the original comment placement.
3. **Correct binding scopes.** `for`/`async for` targets, `with` bindings,
   `except ... as e` names, walrus targets, comprehension targets and match
   captures are treated as bindings — they are no longer rewritten as if they
   were module-level references (2.x rewrote them, which could break local
   bindings that happened to shadow a module name). Function bodies stay
   lazy (runtime references) while parameter defaults, decorators, class
   bodies and return/parameter annotations stay eager (import-time
   references) for ordering purposes.
4. **Exact one-pass consistency.** The same walker (scope machinery included)
   performs reference collection and renaming, so what is *measured* is
   exactly what is *rewritten*.
5. `if __name__ == "__main__":` detection, `TYPE_CHECKING` extraction,
   `try/except` import guards and `__all__` mutation tracking all operate on
   typed CST nodes instead of regexes over tree-sitter text.

## Internal notes

* `models.Block` stores `nodes` (libcst statements) + `refs`; the `source`
  property renders via `cst.Module` for hashing and tests.
* `parser.RefWalker` is a single `cst.CSTTransformer` with a `collect` flag:
  `collect=True` records `Ref`s (kind `name` / `attr` / `nested_import`,
  eager/lazy), `collect=False` rewrites against the resolver environment.
* Docstring and comment stripping happen before extraction; libcst 1.x
  `FormattedString` nodes keep non-interpolated f-strings classified as bare
  strings (parity with 2.x).
* Annotations are treated as eager references even under
  `from __future__ import annotations` (2.x marked them lazy). This only
  ever makes the emit order more conservative, never incorrect.

# Changes in 2.1.0

## Critical fixes

1. **All dataclasses were field-less** (`models.py`, `core.py`, `resolver.py`).
   The previous sources declared dataclass fields without annotations
   (`skipped = field(default_factory=list)` instead of
   `skipped: list = field(...)`). Without annotations those lines are plain
   class attributes, every dataclass accepted zero constructor arguments and
   the positional `ConsolidationResult(...)` / `ImportSpec(...)` /
   `Block(...)` calls raised `TypeError` — the tool could not run at all.
   All fields are now properly declared.
2. **`--no-keep-docstrings` corrupted block sources** (`parser.py`).
   Docstrings were deleted line-wise *after* occurrence byte-offsets had been
   collected, without remapping the offsets, so every subsequent rewrite hit
   the wrong positions. Docstrings are now stripped module-wide *before*
   parsing (the module is re-parsed afterwards), and function/class
   docstrings — previously never stripped at all — are removed too.
3. **Comment stripping broke multi-line strings** (`parser.py`).
   The line-based stripper removed any line starting with `#`, including
   lines *inside* triple-quoted strings. Stripping is now token-based
   (`tokenize`), with a safe no-op fallback.
4. **CRLF was reported as normalized but never was** (`parser.py`). CRLF is
   now actually converted to LF so no stray `\r` leaks into the output.

## Behavior fixes

5. The consolidated file now **emits `__all__`**: when the top-level package
    `__init__` declares `__all__`, the generated module re-emits it with
    names mapped through any renames, so `from consolidated import *` and
    star-import semantics match the original package exactly.

6. Data-file relative paths were computed against the import base, adding a
   spurious package prefix (`output/pkg/data/x.json` instead of
   `output/data/x.json`) when the source is a single package directory
   (`discovery.py`). They are now relative to the directory you point at.
7. `discover_data_files` used exact set membership for default file globs, so
   patterns like `test_*.py` never matched; it now uses `fnmatch` and also
   skips symlinked files, and returns a deterministically sorted list.
8. The `__name__` semantic warning no longer fires for every module that has
   an `if __name__ == "__main__":` guard (main-guard regions are excluded
   from the scan).
9. Class-body attribute names are no longer collected as re-writable names —
   rewriting them would break `self.attr` access.
10. `topo_order` cannot raise `ValueError`/loop forever on malformed inputs
   (defensive candidate handling; `heaps.get(cur)` avoids polluting the heap
   dict).
11. `--main-name` / invalid `imports_mode` values are validated up front.
12. Default output filename is now derived from the *source* name instead of
    the current working directory (more predictable for single-file inputs).
13. The ruff formatter no longer leaks its temporary file on failure
    (`writer.py`), and its timeout was raised from 5s to 30s.
14. CLI passed both `strip_comments=not keep_comments` and `keep_comments`;
    the API now has a single `keep_comments` parameter.

## New functionality: data embedding

15. `consolidate(..., embed_data=True)`` / CLI `--embed-data` embeds data
    files into the consolidated module (new `resources.py`):
    * text files (json, ini, yaml, csv, ...) as plain-text string literals;
    * binary files (fonts, sqlite, ...) as base64 literals, chunked across
      lines.
    The module gains `get_resource(name, *, encoding=None)`,
    `resource_path(name)` (temp-file context manager) and `list_resources()`;
    accessor names are auto-renamed if they would collide with project names.
    `ConsolidationResult.embedded_data_files` reports what was embedded.

## Optimizations

16. `_build_envs` scanned the full `def_block` table once per module
    (O(modules × defs)); definitions are now grouped per module up front.
    Nested-import membership uses a set instead of repeated list scans.
17. The TOML config reader is shared (`discovery.read_pyproject_section`)
    instead of being duplicated in `core.py` and `discovery.py`.
18. The third-party `loguru` dependency was replaced by a small
    stdlib-`logging` shim with the same call surface
    (`logging_setup.py`) — install footprint is now tree-sitter only.
19. Unused imports and dead type-imports removed across all modules; all
    signatures re-annotated.
