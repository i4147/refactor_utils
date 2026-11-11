# Known limits

- **Renaming is name-based.** Parameters and class bodies are scoped, other
  local variables are not. A local variable that has the same name as a
  renamed module-level object may be rewritten too. A full scope analysis is
  future work.
- **Strings are not rewritten.** Names inside string literals, including
  string annotations and `getattr(module, "name")`, keep their original
  spelling.
- **Single-file semantics.** `__file__`, `__name__` and `__package__` mean
  something different in one file. A warning is logged; users of
  `importlib.resources` may need `files()`.
- **Guarded imports** (`try/except ImportError`, `if sys.platform ...`) are
  kept verbatim and may have side effects.
- **Dynamic imports** through `importlib` or `__import__` are warned about,
  not resolved.
- **Compound module-level blocks** that mix imports and definitions are
  emitted verbatim; names bound inside are not rewritten.
- **Validation is syntax-only.** A result that parses can still fail at
  import time. See [Validation](internals/validation.md).
- **Unresolvable internal imports** are dropped with a warning; the names they
  provided stay unresolved.
