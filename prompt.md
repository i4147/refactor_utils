- dedup constant definitions
- wrong __all__ in output, so skip constant named __all__

# Edge cases not considered in project

## 1. Correctness bugs (produce wrong output silently)

### 1.1 `from __future__` imports are unioned across *all* modules
In `resolver.build`, `self.future_names` is a union over every parsed module. If module A has `from __future__ import annotations` and module B does not, the consolidated file gets **lazy annotations everywhere** — a real semantic change for B. Worse for `barry_as_FLUFL`, `generator_stop`, etc.

Fix: warn per-module when the set is not uniform, or only emit when *all* modules agree.

### 1.2 `__init__.py` re-exports disappear
`_exports` only iterates `kept` **blocks**. In:
```python
# __init__.py
from .sub import helper as public_helper
__all__ = ["public_helper"]
```
`public_helper` is never a block, so `plan.exports` is empty and `render_init` emits nothing. Users of `from pkg import public_helper` get `ImportError`. This is *the* canonical `__init__.py` pattern — re-exporting under a different name is silently dropped.

Fix: track `ImportSpec`s whose `local_name` is public and whose target resolves to a `def`/`ext`, and include them in `plan.exports`.

### 1.3 `__all__` accumulation is wrong
```python
info.declared_all = (info.declared_all or []) + _STR_RE.findall(...)
```
Multiple `__all__ = [...]` assignments **replace**, they don't append. `__all__ = ['a']; __all__ = ['b']` yields `['a','b']` here instead of `['b']`. `+=` should append but `=` must replace.

Also: `__all__ = []` collapses to falsy and is then treated as "not declared" in `_exports`.

### 1.4 Dunders are not exported
`_exports` skips `fin.startswith("_")`, so `__version__`, `__author__`, `__all__` (as a name) never make it into `render_init`. Common `from pkg import __version__` breaks.

Fix: allow `__version__`, `__author__`, `__email__`, `__license__`, `__copyright__` (and anything explicitly in `__all__`) through the filter.

### 1.5 `@overload` groups break under renaming
`_assign_names` groups by `primary` name, so:
```python
@overload
def f(x: int) -> int: ...
@overload
def f(x: str) -> str: ...
def f(x): return x
```
becomes three blocks all named `f`; blocks 1 and 2 get renamed to `mod_f` and `mod_f_2`. `@overload`-matching in mypy/typing is destroyed.

Fix: detect `@overload` and treat consecutive same-name blocks as one atomic block; or at least skip rename for overload groups and only rename the implementing definition's *references*.

### 1.6 TYPE_CHECKING blocks lose non-import statements
In `_extract`:
```python
if cond in _TC_NAMES:
    body = n.child_by_field_name("consequence")
    for ch in body.named_children ...:
        if ch.type in _IMPORT_TYPES:
            info.imports.extend(parse_import(...))
    continue
```
Anything else inside `if TYPE_CHECKING:` (type aliases, `X: TypeAlias = ...`, `Protocol` stubs, `TypeVar` definitions) is **silently dropped**. That's a nasty data-loss bug because TYPE_CHECKING blocks often hold exactly those aliases.

### 1.7 Try/except ImportError guards with internal imports are broken
`_extract` collects these as `GuardBlock`s and they're emitted verbatim in the header of the consolidated file. But `from .foo import bar` inside a consolidated `_consolidated.py` has no `.foo` module to import from. Worse: `_render_block` doesn't rewrite guards, and ordering never considers their dependencies. Result: runtime `ImportError` in the output that wasn't there in the input.

Fix: run `parse_import` on guard specs, resolve internal ones, and either (a) hoist the import out of the guard into the body, or (b) raise a hard error when `--mode single|flat|module` with internal guarded imports.

Same problem for `sys.version_info`-conditional imports (warned, but kept verbatim with internal targets).

### 1.8 Compiled extension imports are silently dropped
```python
tgt = self._spec_target(spec, set())
if tgt is None:
    if self._is_internal(spec):
        self._warn_once(f"...cannot resolve internal import...")
    continue
```
If `foo/_ext.so` exists and is imported by `foo.py`, `_is_internal("foo._ext")` returns `True` (because `foo` is in `tops`) but the symbol can't be resolved, so the import is dropped instead of kept verbatim. Result: broken output for any package with C/Rust extensions.

Fix: when `_is_internal` is `True` but `_resolve_symbol` returns `None`, keep the import verbatim (and warn).

### 1.9 `--generate-guard` doesn't handle async `main`
```python
guard_text = (
    'if __name__ == "__main__":
    main()'
    if not self.main_name
    else f'if __name__ == "__main__":
    {self.main_name}()'
)
```
If `main` is `async def`, the guard calls an unawaited coroutine and returns immediately. Needs `asyncio.run(main())`. Same for any `--main-name X` where `X` is async.

### 1.10 `__all__` regex is wrong for non-ASCII and concatenation
`_STR_RE = re.compile(r"""['"]([A-Za-z_][A-Za-z0-9_]*)['"]""")`:
- Doesn't match unicode identifiers (`"café"` is a valid `__all__` entry in Python 3).
- Matches `"a" + "b"` as `["a", "b"]` instead of failing.

Fix: parse `__all__` via the AST of the RHS instead of regex; bail out to a warning if it's not a literal list/tuple/set of strings.

---

## 2. Semantic changes that are not warned about

### 2.1 `pickle` / `dill` / `cloudpickle` of module-level objects
Consolidation changes `func.__module__` and `cls.__module__` to the new module. Any pickled artifact referencing the old dotted path will not unpickle. There's no warning for `pickle`, `dill`, `cloudpickle`, `shelve`, or `multiprocessing` usage. This is the single biggest "surprise breakage" for downstream users.

### 2.2 `logger = logging.getLogger(__name__)` per module
Very common. After consolidation `__name__` is `_consolidated` for *every* block. Logger hierarchy breaks. `_detect_runtime_semantics` checks for `__name__` but doesn't warn specifically about this. Should warn + optionally rewrite `getLogger(__name__)` to `getLogger(__name__ + ".<orig_module>")`.

### 2.3 `get_type_hints()` on functions from modules that didn't have `from __future__ import annotations`
Combined with 1.1: if any input had lazy annotations, all functions get lazy annotations. `get_type_hints()` still works if all referenced types are visible in the consolidated module globals — which they are, but only if the rename rewrite got the annotation right. The interaction with string forward refs (2.4) makes this fragile.

### 2.4 Forward references in string annotations are not rewritten
```python
def f(x: "Foo") -> "Bar": ...
```
`Foo`/`Bar` inside strings are not collected as `Occ`s. If `Foo` is renamed (name collision), the string is stale and `get_type_hints()` will fail. Fix: recursively walk string annotation nodes with `ast.parse`, or at minimum warn.

Same for `typing.cast("Foo", x)`, `TypeVar(bound="Foo")`, `@dataclass` field defaults in string form.

### 2.5 `from . import utils; utils.helper()`
`_resolve_dotted("__init__", "utils", "helper")` returns the `helper` block and `_render_block` rewrites `utils.helper` → `helper` (correct for post-consolidation). But **any non-attribute usage** of `utils` (as an object) breaks: `print(utils)`, `vars(utils)`, `getattr(utils, "helper")`, `isinstance(x, utils.SomeClass)` — wait, `isinstance` is fine (attribute access). But `print(utils)` prints the module — after consolidation `utils` is `NameError` because the module was pruned and `utils` was never bound.

`_target_name(("mod", "utils"), "utils")` returns `None`, so nothing is rewritten, and the import is pruned. Output: `NameError: utils`.

Fix: if any occurrence of a module alias is non-attribute, keep the import **and** the module name as a synthetic binding (`import utils` pointing at nothing won't work in single-file mode, so this must fail loudly). Better: emit an error and require `--preserve-modules`.

### 2.6 `__file__`-relative resource access — warned but broken
`_detect_runtime_semantics` warns for `__file__` and `importlib.resources`/`pkg_resources`, but `Path(__file__).parent / "data.json"` will look in the wrong directory. `--copy-data` only copies data files to `output/`; it doesn't rewrite the `__file__` line. Users will get `FileNotFoundError` and have no obvious fix short of hand-editing.

Fix: emit a hard error unless `--allow-resource-breakage`; or rewrite `Path(__file__).parent` → `Path(__file__).parent / "data_dir"` when `--copy-data` copies to a known subdir. At minimum, make the warning prominent and mention `--copy-data`.

### 2.7 `globals()`/`locals()`/`getattr(module, "name")`
Warned, but still broken — string keys are not rewritten. Users relying on `globals()["FOO"]` will get `KeyError` after rename. A hard error or `--strict` promotion is safer than a warning.

---

## 3. Parser gaps (missed bindings → possible mis-renames)

### 3.1 `match` / `case` pattern bindings
`_bound_names` treats `match_statement` as `_COMPOUND` but doesn't descend into case patterns, so `case Point(x=x, y=y): ...` doesn't record `x`/`y` as bindings. `_collect_occs` doesn't shadow them either. If a module-level `x` is renamed, the pattern binding and its uses inside the case body both get rewritten — usually consistent, but `match` patterns with guards (`case Point(x=x) if x > 0:`) reference the just-bound `x`, which the shadow set won't know about. If another module also defines `x`, you could rename the *inner* case binding and break the guard.

### 3.2 `as` bindings in `with` / `except`
`with open(p) as f:` / `except E as e:` — `f` and `e` aren't added to shadow, so a module-level `f` rename incorrectly rewrites `f` inside the `with` body. Same consistent-rename argument applies, but unlike `def`/`lambda` parameters, the *purpose* of the rename (`foo` was module-level) is now confusing at the call site.

### 3.3 Walrus in comprehensions
`[y := f(x) for x in ...]` — `y` is a binding but not shadowed, `x` is a loop binding not shadowed. Consistent-rename saves semantics, but `_bound_names` doesn't detect these, so anything relying on `_bound_names` (e.g., if a future feature exports `y`?) would be wrong. Low priority.

### 3.4 `nonlocal`
Not tracked at all. Currently harmless because the rename is consistent, but combined with 3.1/3.2 it can flip a name from local to "free" in the wrong scope.

### 3.5 Non-UTF8 source / encoding declarations
`src.decode("utf-8", "replace")` for tree-sitter and `_extent`-sliced bytes re-decoded as UTF-8 → latin-1/GBK sources become mojibake in the output. Only warns. Fix: honor `# -*- coding: X -*-` (first two lines) with `tokenize.detect_encoding`, or hard-fail on decode errors unless `--allow-syntax-errors`.

---

## 4. Discovery / IO issues

### 4.1 Symlinks + target both parsed
`_walk` yields the symlink *and* its target if both are under the tree. `parsed[module]` is keyed by module name, so one silently overwrites the other. Fix: dedupe by `Path.resolve()`.

### 4.2 `__main__.py` excluded, but package consolidation expects it
`EXCLUDED_FILE_GLOBS` contains `__main__.py`. So `python -m pkg` post-consolidation is broken because `pkg/__main__.py` was never parsed. Either include it (as an entry-only MAIN block) or warn loudly that `python -m` support is lost.

### 4.3 Output directory is a file
`output.mkdir(parents=True, exist_ok=True)` raises `FileExistsError` with a confusing message if `output` is a file. Should check `output.is_file()` up front with a `ConsolidationError`.

### 4.4 `--check` with missing output succeeds
`if existing.is_file(): ... else: logger.info("No existing ... to diff")` → `--check` passes when there's nothing to check. CI users expect this to fail (like `--check` in `isort`/`black`). At minimum make it explicit and configurable.

### 4.5 `--validate-import` executes the module
`spec.loader.exec_module(mod)` runs top-level code — could open files, start threads, make HTTP calls. This should print a prominent warning before execution, or be split into `--validate-compile` (default) vs `--validate-execute`.

### 4.6 Line endings normalized silently (warned but not fixed)
`b"\r"` detection adds a warning, then `writer` joins with `"\n"`. On Windows, this is fine; on a repo with `.gitattributes` enforcing `eol=crlf` for `*.py`, every consolidation step creates a spurious diff. Consider an `--eol {lf,crlf,preserve}` flag.

---

## 5. Design/architecture concerns

### 5.1 Whole-file rewriting via byte offsets is fragile
`_render_block` applies `(start, end, replacement)` triples to the original byte slice. If any occ spans a boundary of another occ (nested occs are prevented, but overlapping sibling occs are not — e.g., `super().__init__()` with a `super` occ and an `attr` occ on the same range), edits silently drop (`if s < last_end: continue`). The `self < last_end` guard is a hidden source of missing rewrites. Suggest: build the AST → rewrite at AST nodes → re-serialize, or at least assert that edits are non-overlapping and log dropped edits at `DEBUG`.

### 5.2 Dedupe can drop semantics-preserving *but not identical* blocks
`safe` dedupe keys on `(kind, primary, source.strip(), ext_refs)`. Two blocks with identical text but different `ext_refs` are treated as distinct — good. But two blocks with different `ext_refs` that happen to resolve to the same final names after `_resolve_ext_conflicts` won't be recognized as duplicates. Minor.

Worse: dedupe runs *before* `_assign_names`, but `ext_refs` is computed from *pre-rename* targets. If two modules both import `from requests import Session`, their `ext_refs` match — but the first import might be aliased differently later. Probably fine, but a post-rename dedupe pass would be more robust.

### 5.3 `topo_order` cycle handling is a warning, not an error
Emitting arbitrary order for a cycle is often fine for deferred references, but if the cycle is via `eager` deps (e.g., class A extends B from another module and B's metaclass touches A), the output will fail at import. `--strict` should promote cycle warnings to errors.

### 5.4 Import section merging loses `try/except` context
`build_import_section` merges all imports into flat blocks. If module A had `try: import foo` (optional dep) and module B had bare `import foo`, they merge into bare. Optional-dependency degradation is lost. `--preserve-modules` should also preserve import guards, or at least keep optional imports inside their try/except.

### 5.5 `star_keys` are emitted unconditionally
External star imports (`from ext import *`) are always emitted, even if the names are unused. That's fine (stars have side-effect potential), but combined with name collisions on the renamed side, star imports can shadow/override consolidated names. `_resolve_ext_conflicts` doesn't consider `star_keys` when picking alias names — a renamed symbol can shadow a star-imported name, changing dispatch. Worth warning.

### 5.6 `_local` parser per thread + thread-pool for rendering
`_render_block` runs in threads, but `_render_block` reads `self.env`, `self.final`, etc. — those are all read-only during rendering, so fine. But `_resolve_dotted` → `_resolve_symbol` also reads — fine. Just flagging that the thread-safety argument holds only because `build()` finishes all mutation before `_render_all` starts. A future refactor could easily break this. Add a comment.

---

## 6. fixes priorities

1. **`__init__.py` re-export loss (1.2)** — silently breaks the most common package pattern.
2. **`from __future__` union (1.1) + string forward refs (2.4)** — silently changes type-checker behavior and breaks `get_type_hints`.
3. **Pickle / `__module__` (2.1)** — silent runtime breakage for downstream consumers; needs at least a warning.
4. **`__all__` accumulation (1.3) + dunder export (1.4)** — output is *wrong*, not just surprising.
5. **TYPE_CHECKING non-import statements (1.6)** — silent code loss.
6. **Async `main` in `--generate-guard` (1.9)** — small, unambiguous fix.
7. **`@overload` renaming (1.5)** — affects a whole class of typed libraries.
8. **Internal imports in `try/except ImportError` (1.7)** — produces broken output that passes `compile()` but fails at import.
