#!/usr/bin/env bash
# demo_consolidate.sh — run pyrefactor over the repo's example packages and show
# the consolidated output (prompt.md section 1 & 2 contract).
#
# Runs are isolated in a temporary copy so the repository stays clean.
#
# Usage: ./arena/demo_consolidate.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -d .venv ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi
export PYTHONPATH="src:${PYTHONPATH:-}"

WORK="$(mktemp -d -t pyrefactor-demo-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

run_demo() {
    local src="$1" label="$2"
    echo
    echo "########################################################################"
    echo "# pyrefactor $label"
    echo "########################################################################"
    pyrefactor "$src" --log-level WARNING
    local out
    out="$src/consolidated"
    echo "--- $out/ ---"
    ls -1 "$out/"
    echo "--- section headers + __all__ ---"
    grep -E '^# (Constants|Classes|Functions|Entry points)|^__all__' "$out"/*.py || true
    python -m py_compile "$out"/*.py && echo "compiles OK"
    echo "--- idempotency check ---"
    pyrefactor "$src" --check --log-level ERROR && echo "--check passes"
}

cp -r examples/data_files_example "$WORK/example"
run_demo "$WORK/example/mypackage" "examples/data_files_example/mypackage"

echo
echo "########################################################################"
echo "# Default '.' invocation from inside a package folder"
echo "########################################################################"
mkdir -p "$WORK/inplace"
cp -r examples/data_files_example/mypackage "$WORK/inplace/mypackage"
(
    cd "$WORK/inplace/mypackage"
    pyrefactor --log-level WARNING
    ls -1 consolidated/
    python -m py_compile consolidated/*.py && echo "compiles OK"
)

echo
echo "########################################################################"
echo "# Package name detection from pyproject.toml (src/ layout)"
echo "########################################################################"
mkdir -p "$WORK/srcproj/src/mypkg"
printf '[project]\nname = "fancy-dist-name"\nversion = "0.1"\n' > "$WORK/srcproj/pyproject.toml"
printf '"""mypkg."""\nfrom .core import hello\n' > "$WORK/srcproj/src/mypkg/__init__.py"
printf 'def hello():\n    return "hi"\n' > "$WORK/srcproj/src/mypkg/core.py"
pyrefactor "$WORK/srcproj/src/mypkg" --log-level WARNING
ls -1 "$WORK/srcproj/src/mypkg/consolidated/"

echo
echo "✔ demo complete"
