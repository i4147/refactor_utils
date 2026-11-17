Key findings so far: **preceding comments attach to the following statement's node** (no manual comment-pulling needed), and **`Module(body=block_body)` deindents automatically**. But `.code` doesn't exist on arbitrary nodes in libcst 1.9 — need a wrap-to-render helper. Fixing the probe for the remaining questions:
`leading_lines: Sequence[EmptyLine]` is the answer — statements carry their preceding comments/blanks, so blocks travel with their comments automatically. Continuing probes:
`.code` exists only on `Module` — all rendering must go through a `Module` wrapper. Adjusting:
All attachment semantics confirmed. Final quick probe for f-strings and statement shapes, then I'll write the new parser:
Probes complete — design settled. Now rewriting `models.py`:
Quick verification of a few node field names before writing the parser:
All shapes confirmed. Now writing the new libcst-based `parser.py`:
The file needs several corrections I already spotted (libcst transformers don't call `leave_*` when `visit_*` returns False; the TYPE_CHECKING helper is convoluted; regex anchoring). Fixing them now:
Now the critical `visit_Import`/`visit_ImportFrom` fix (leave is not called when visit returns False in libcst):
Now replacing the convoluted `_extract`/`_add_type_checking` section with a clean version:
The old one-argument `_detect_runtime_semantics` is now duplicated — removing the stale version:
Parser done. Now adapting `resolver.py`:
Now the `_render_block` rewrite — the core change from string surgery to CST transformation:
`AsyncFor`/`AsyncWith` don't exist — async is a flag on `For`/`With`. Fixing `_COMPOUND` and walker aliases:
Package imports cleanly. Quick parser smoke test before the full suite:
`PositionProvider` yields `CodeRange` — need `.start.line`:
Mostly working — but function-body refs come out `eager=True` and param names aren't shadowed, so the func-scope push isn't happening. Debugging:
Found it — the scope machinery works; the stray ref is the **param name itself** being visited as a Name while still in base ctx. Param names are bindings — skipping them in `visit_Parameters`:
The skip machinery works correctly — the `item` ref comes from its legitimate *use* in the body (parity with the original). Parser is sound. Now updating version metadata and docs:
Now the `__all__` mutation fix in `_add_simple`, and the `.occs`→`.refs` test fix:
All 49 tests pass, but the demo breaks: a deduped `shared()` gets renamed inconsistently in the merged guard. Debugging the rename flow:
Found it: the renamer skips def **names** (correct for collection, wrong for renaming — 2.x collected the own def's name precisely so it could be renamed). Fixing `leave_FunctionDef`/`leave_ClassDef` to rename def names in rename mode:
All green. Adding libcst-specific regression tests (comment travel + binding scopes) to lock in the new guarantees:
Two fixes: `_extract` looks up positions for a node it replaced (KeyError), and my new test asserted a rename that shouldn't happen. Fixing both:
All 51 tests pass, demo passes. One more sanity check of the strip transforms + formatting via CLI flags:
Strip/format pipelines verified (remaining `"""`/`#` are the generated header and section rules). Packaging:
Done. The project is rewritten on **libcst** and verified: extracted fresh from the archive → **51 tests pass**, demo passes, pyflakes clean.

**[pyrefactor-3.0.0.tar.xz](/home/z/my-project/download/pyrefactor-3.0.0.tar.xz)** (the previous tree-sitter-based [pyrefactor-2.1.0.tar.xz](/home/z/my-project/download/pyrefactor-2.1.0.tar.xz) remains in `download/` for comparison)

## What changed

**Dependency**: `libcst>=1.1` replaces `tree-sitter`/`tree-sitter-python` entirely. CLI surface, library API, consolidation semantics and data-embedding are unchanged.

**Pipeline rework** (the whole tree-sitter layer is gone):

- **Blocks hold CST nodes, not byte ranges** — `Block.nodes` are libcst statements; `Occ` byte-offset records became scope-aware `Ref` records (`name`/`attr`/`nested_import`, eager/lazy). Renames, nested-import replacement, docstring stripping and comment stripping are node-level transforms, so the whole class of 2.x offset-desync bugs is impossible by construction.
- **One walker, both jobs** — `parser.RefWalker` is a single `CSTTransformer` with a `collect` flag: collection (ordering deps) and renaming share the exact same scope machinery, so what is measured is exactly what is rewritten. Function bodies stay lazy (runtime), while decorators, class bodies and parameter defaults stay eager (import-time) for ordering.
- **Correct bindings** — `for`/`with`/`except as`/walrus/match-capture targets and param names are treated as bindings and never rewritten (2.x rewrote them as if they were module references). Covered by a new regression test.
- **Lossless comments** — libcst attaches inter-statement comments to the following statement (`leading_lines`), so comments travel with their block when reordered; new test locks this in.
- Main-guard / `TYPE_CHECKING` / try-except-guard / `__all__` detection now work on typed nodes instead of regexes over text.

## Fixes found during the rewrite

- libcst transformers don't call `leave_*` when `visit_*` returns `False` — import handling descends with skipped names instead of short-circuiting
- def/class **names** must be renamed in rename mode (they're skipped only during collection — this broke the demo's `helpers_shared` rename initially)
- `WithItem.asname` (not `binding`), `RemovalSentinel.REMOVE` (not `RemoveFromPad`), `CodeRange.start.line`, and async loops living in `For(asynchronous=...)`

One deliberate semantic note (documented in `CHANGES.md`): annotations are treated as eager references even under `from __future__ import annotations` — this only makes emit order more conservative, never incorrect.