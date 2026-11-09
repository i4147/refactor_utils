#!/usr/bin/env bash
# setup.sh — create the venv and install runtime + test + docs dependencies.
#
# Usage:
#   ./arena/setup.sh              # create .venv and install everything
#   USE_SYSTEM_PYTHON=1 ./arena/setup.sh   # install into the active interpreter
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "${USE_SYSTEM_PYTHON:-0}" != "1" ]]; then
    if [[ ! -d .venv ]]; then
        echo "=== Creating .venv ==="
        python3 -m venv .venv
    fi
    # shellcheck disable=SC1091
    source .venv/bin/activate
    PIP=(python -m pip install --quiet --upgrade)
else
    PIP=(python -m pip install --quiet --upgrade --break-system-packages)
fi

echo "=== Installing refactor_utils + autoall (editable) ==="
"${PIP[@]}" pip
"${PIP[@]}" -e . -e autoall

echo "=== Installing runtime/test deps ==="
"${PIP[@]}" loguru tree-sitter tree_sitter_python pytest pytest-cov tqdm

echo "=== Installing docs deps ==="
"${PIP[@]}" "mkdocs>=1.5" "mkdocs-material>=9" "mkdocstrings[python]>=0.24" \
    "pymdown-extensions" fpdf2 pypdf

echo "=== Installing dev helpers (formatters, docstring patcher) ==="
"${PIP[@]}" black ruff libcst

echo "=== Sanity check ==="
python - <<'EOF'
import autoall, refactor_utils
print("refactor_utils", refactor_utils.__version__)
print("autoall", autoall.__version__)
EOF
python -m py_compile src/refactor_utils/*.py autoall/src/autoall/*.py
echo "compile OK"

echo "✔ setup complete — try: ./arena/run_tests.sh"
