#!/usr/bin/env bash
# demo_consolidate.sh — Run pyrefactor over the repo's example packages and
# show the consolidated output (prompt.md section 1 contract).
# Usage: ./arena/demo_consolidate.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH=src

run_demo() {
    local src="$1"
    echo
    echo "########################################################################"
    echo "# pyrefactor $src"
    echo "########################################################################"
    rm -rf "$src/consolidated"
    pyrefactor "$src" --log-level ERROR
    echo "--- $src/consolidated/ ---"
    ls -1 "$src/consolidated/"
    echo "--- head of consolidated/<pkg>.py ---"
    head -25 "$src/consolidated/"*.py
    python -m py_compile "$src/consolidated/"*.py && echo "compiles OK"
}

run_demo examples/data_files_example/mypackage
run_demo examples/config_example 2>/dev/null || true

echo
echo "### Default '.' invocation from inside a package folder ###"
cd examples/data_files_example/mypackage
rm -rf consolidated
pyrefactor --log-level ERROR
ls -1 consolidated/
python -m py_compile consolidated/*.py && echo "compiles OK"

echo "✔ demo complete"
