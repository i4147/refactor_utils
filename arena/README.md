# Arena helper scripts

Helper scripts (bash & python) used during this arena session to develop,
test, and document the `refactor_utils` changes from `prompt.md`:

- default `pyrefactor [pkg]` → `consolidated/<pkgname>.py` (single file)
- pkg name detection from `pyproject.toml`/`setup.cfg`/`setup.py`
- temporary `const.py`/`classes.py`/`func.py` extraction + `autoall.__all__`
- docs (HTML + single-file PDF) and test coverage for the new flow

Run from the repository root (`/home/user/refactor_utils`).

## Contents

| Script | Purpose |
|--------|---------|
| `setup.sh` | Install the project (editable) + test/docs dependencies |
| `run_tests.sh` | Run the pytest suite |
| `build_docs.sh` | Build the mkdocs HTML site |
| `build_pdf.py` | Build the single-file `refactor_utils.pdf` |
| `check_all.sh` | Full session CI: tests + docs + PDF + demo consolidation |
| `demo_consolidate.sh` | Run `pyrefactor` over the example packages |
| `helpers.py` | Python helpers: make demo pkgs, validate generated files |

## Quick start

```bash
./arena/setup.sh
./arena/run_tests.sh
./arena/build_docs.sh
python arena/build_pdf.py
./arena/demo_consolidate.sh
```
