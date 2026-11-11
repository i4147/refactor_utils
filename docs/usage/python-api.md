# Python API

```python
from refactor_utils import (
    ConsolidationError,
    ConsolidationResult,
    consolidate,
    validate_syntax,
)
```

## consolidate

```python
from pathlib import Path
from refactor_utils import ConsolidationError, consolidate

try:
    result = consolidate(
        Path("src/mypackage"),
        Path("output"),
        filename="mypackage",
        imports_mode="prune",
        copy_data=False,
    )
except ConsolidationError as exc:
    raise SystemExit(f"failed: {exc}")

print(result.files)       # [PosixPath('output/mypackage.py')]
print(result.exports)     # number of public names
for module, old, new in result.plan.renamed:
    print(f"{module}: {old} -> {new}")
```

Keyword arguments mirror the command line:

| Argument | Default | CLI flag |
|----------|---------|----------|
| `filename` | current directory name | `--name` |
| `workers` | CPU count | `-j` |
| `executor` | `"process"` | `--executor` |
| `exclude`, `include` | `()` | `-x`, `--include` |
| `imports_mode` | `"prune"` | `--imports` |
| `entry` | `()` | `--entry` |
| `entry_keep_all` | `False` | `--entry-keep-all` |
| `main_name` | `None` | `--main-name` |
| `generate_guard` | `False` | `--generate-guard` |
| `keep_docstrings` | `True` | `--keep-docstrings` |
| `keep_comments` | `True` | `--keep-comments` |
| `do_format`, `formatter` | `False`, `"black"` | `--format`, `--formatter` |
| `copy_data`, `data_glob` | `True`, `()` | `--copy-data`, `--data-glob` |
| `dry_run`, `check`, `stdout`, `diff`, `force` | `False` | same names |
| `config` | `None` | `--config` |
| `progress` | `False` | `--progress` |

## ConsolidationResult

| Field | Meaning |
|-------|---------|
| `output_dir` | output directory |
| `files` | generated files (empty for `dry_run`, `check`, `stdout`) |
| `modules` | number of consolidated modules |
| `exports` | number of public names in the result |
| `warnings` | parser warnings |
| `diff` | unified diff text when `diff=True` |
| `stdout_text` | the generated module when `stdout=True` |
| `copied_data_files` | data files copied to the output |
| `validated` | `True` once the syntax check passed |
| `dry_run` | `True` when nothing was written |
| `plan` | the `Plan`: rendered blocks, `renamed`, `deduped`, `exports` |

## validate_syntax

```python
validate_syntax(text: str, filename: str = "<consolidated>") -> ast.Module
```

Parses `text` with `ast.parse`. Returns the tree, or raises
`ConsolidationError` naming the file, the line and the offending source line.
The code is never executed or imported.

```python
from refactor_utils import ConsolidationError, validate_syntax

validate_syntax("x = 1\n")                      # ok

try:
    validate_syntax("class 2_Tokenizer:\n    pass\n", "result.py")
except ConsolidationError as exc:
    print(exc)
# generated result.py is not valid python: invalid decimal literal (line 1): 'class 2_Tokenizer:'
```

`consolidate` calls it on every generated module before writing. See
[Validation](../internals/validation.md).

## Naming helpers

`refactor_utils.resolver` exposes the two functions used for every generated
name:

```python
from refactor_utils.resolver import is_valid_identifier, make_identifier

make_identifier("2")         # 'm2'
make_identifier("my-mod")    # 'my_mod'
make_identifier("class")     # 'class_'
is_valid_identifier("2_x")   # False
```
