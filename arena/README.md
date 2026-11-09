# Arena helper scripts

Helper scripts (bash & python) used during this arena session to implement,
test and document the changes requested in `prompt.md`:

- `pyrefactor [pkg path]` → `consolidated/<pkgname>.py` (single file, path
  defaults to `.`);
- package-name detection from `pyproject.toml`/`setup.cfg`/`setup.py` in the
  package folder and up to two levels above (fallback: folder name);
- constants → `const.py`, functions → `func.py`, classes → `classes.py`
  temporary extraction, optimized imports, merge, cleanup;
- `autoall.ensure_all_file()` appends `__all__` in place (idempotent, CRLF
  safe, user `__all__` respected);
- full test suite, docs (HTML + single-file PDF) and per-file documentation.

Run everything from the repository root (`/home/user/refactor_utils`).

## Contents

| Script | Purpose |
|--------|---------|
| `setup.sh` | create `.venv`, editable-install `refactor_utils` + `autoall`, install test/docs/dev deps |
| `run_tests.sh` | run pytest (`--cov` / `--verbose` optional) |
| `check_all.sh` | full session CI: tests → prompt contract → docstrings → HTML docs → PDF → real packages → demos |
| `demo_consolidate.sh` | run `pyrefactor` on the example packages (temp copies, repo stays clean) |
| `test_on_packages.sh` | run `pyrefactor` on real packages (this repo, `autoall`, `loguru`, `fpdf`, `jinja2`, `markdown`) and report modules/size/time/compile/check |
| `verify_prompt.py` | 15 assertions covering the `prompt.md` contract (layout, name detection, ordering, temp files, `__all__`, `--check`, `-o`) |
| `docstring_audit.py` | report missing module/class/function docstrings (`--tests` includes the suite) |
| `add_docstrings.py` | libcst script that inserted the 68 missing docstrings (idempotent, `--check` dry run) |
| `build_docs.sh` | `mkdocs build --strict` (`--serve` for a live preview) |
| `build_pdf.py` | build `refactor_utils.pdf` from the mkdocs nav with fpdf2 (no browser needed) |
| `helpers.py` | python helpers to create demo packages and validate generated files |

## Quick start

```bash
./arena/setup.sh
./arena/run_tests.sh
python arena/verify_prompt.py
./arena/test_on_packages.sh
./arena/build_docs.sh
python arena/build_pdf.py
./arena/check_all.sh      # everything at once
```

## What the last run reported

```
$ ./arena/test_on_packages.sh
TARGET           PKG NAME           MODULES      SIZE   SECONDS COMPILES CHECK
refactor_utils   refactor_utils          11      296K     0.38s      YES CLEAN
autoall          autoall                 1       28K     0.14s      YES CLEAN
mypackage        mypackage               4      4.0K     0.13s      YES CLEAN
loguru           loguru                 19      184K     0.26s      YES CLEAN
fpdf             fpdf                   37      1.4M     1.44s      YES CLEAN
jinja2           jinja2                 25      476K     0.52s      YES CLEAN
markdown         markdown               31      300K     0.33s      YES CLEAN
✔ 7 package(s) consolidated, compiled and checked cleanly
```
