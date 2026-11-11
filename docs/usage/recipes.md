# Recipes

## Consolidate the current project

```bash
pyrefactor .
```

Writes `./output/<cwd>.py`.

## Stable file name in CI

```bash
pyrefactor src/mypackage --name mypackage --check
```

`--check` exits with code 2 when the committed output is stale.

## Preview without writing

```bash
pyrefactor src/mypackage --dry-run
pyrefactor src/mypackage --stdout | less
```

## Skip vendored code

```bash
pyrefactor . -x "vendor/*"
```

## Include the tests directory

```bash
pyrefactor . --include "tests/*"
```

## Format the result and drop comments

```bash
pyrefactor . --format --formatter ruff --no-keep-comments
```

## Keep a single entry point

```bash
pyrefactor . --entry cli
```

Only the `if __name__ == "__main__":` guard of the module `cli` survives.

## Code only, no data files

```bash
pyrefactor . --no-copy-data
```

## See what was renamed

```bash
pyrefactor . --dry-run 2>&1 | grep Renamed
```

```text
INFO | Renamed 2:Tokenizer -> m2_Tokenizer
```

## Check any Python file with the same validator

```bash
python tools/check_syntax.py output/
```
