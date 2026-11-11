# Validation

Before anything is written, the generated module is checked by one small
function:

```python
def validate_syntax(text: str, filename: str = "<consolidated>") -> ast.Module:
    ...
```

It calls `ast.parse(text)`. That is all.

## Guarantees

- **Always on.** There is no flag. It runs for normal runs, `--stdout`,
  `--dry-run`, `--check` and `--diff`.
- **Before writing.** On failure nothing is created or overwritten: not the
  output directory, not the Python file, not the data files.
- **After formatting.** With `--format` the formatted text is what gets
  checked.
- **Syntax only.** The code is parsed, never compiled into a module, executed
  or imported.

## What it does not check

Because nothing is executed, these do **not** fail validation:

- a third-party dependency that is not installed;
- module-level code that raises (for example a `@dataclass` with fields that
  lack annotations);
- undefined names, wrong arguments, type errors.

This is intentional. The former validator also smoke-imported the result,
which made runs fail for reasons unrelated to the consolidation itself. It
has been removed together with the `--validate`, `--validate-import` and
related flags.

If you want the stricter check, run it yourself:

```bash
python -c "import runpy; runpy.run_path('output/myproject.py')"
```

## Failure

```text
ERROR | generated result.py is not valid python: invalid decimal literal (line 590): 'class 2_Tokenizer:'
```

The process exits with code 2. From Python, `consolidate()` raises
`ConsolidationError` with the same message.

A failure here means `pyrefactor` produced broken code from valid input. That
is a bug in the tool: please report it together with the message.

## Input files

Source files are checked too, earlier and by tree-sitter: a source file with
a syntax error aborts the run and is listed in the error message.

## Using the validator elsewhere

```python
from refactor_utils import validate_syntax
validate_syntax(open("some_file.py").read(), "some_file.py")
```

or from the shell:

```bash
python tools/check_syntax.py some_file.py some_directory/
```
