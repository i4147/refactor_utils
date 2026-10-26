# Arena Helper Scripts

This directory contains the helper scripts (bash & python) used during the arena session to develop, test, and document `refactor_utils`.

All scripts are runnable from the repository root (`/home/user/refactor_utils`).

## Contents

| Script | Purpose |
|--------|---------|
| `setup.sh` | Install project in editable mode + dependencies |
| `run_tests.sh` | Run pytest suite (with coverage) |
| `build_docs.sh` | Build mkdocs site (`--strict`) |
| `serve_docs.sh` | Serve mkdocs locally |
| `run_examples.sh` | Execute all example scripts |
| `consolidate_self.sh` | Consolidate the project onto itself (self-test) |
| `consolidate_examples.sh` | Consolidate example packages |
| `check_all.sh` | Full CI: tests + docs + examples + self-consolidation |
| `helpers.py` | Python helpers for project creation, data file checks, validation |
| `test_helpers.py` | Python helpers used for manual discovery/parser/resolver testing |
| `git_helpers.sh` | Git/PR helpers used during session |

See each script header for usage.
