#!/usr/bin/env bash
# setup.sh — Install refactor_utils (editable) + runtime/test/docs deps.
# Usage: ./arena/setup.sh
# setup.sh — create the venv and install runtime + test + docs dependencies.
#
# Usage:
#   ./arena/setup.sh              # create .venv and install everything
#   USE_SYSTEM_PYTHON=1 ./arena/setup.sh   # install into the active interpreter
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== Installing refactor_utils (editable) ==="
pip install -e . --break-system-packages --quiet
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

echo "=== Installing runtime/test extras ==="
pip install --break-system-packages --quiet pytest pytest-cov tqdm
echo "=== Installing refactor_utils + autoall (editable) ==="
"${PIP[@]}" pip
"${PIP[@]}" -e . -e autoall

echo "=== Installing docs extras ==="
pip install --break-system-packages --quiet "mkdocs>=1.4" "mkdocs-material>=9" "mkdocstrings[python]>=0.5" fpdf2 pypdf
echo "=== Installing runtime/test deps ==="
"${PIP[@]}" loguru tree-sitter tree_sitter_python pytest pytest-cov tqdm

echo "=== Installing autoall (sibling package used for __all__) ==="
pip install -e autoall --break-system-packages --quiet
echo "=== Installing docs deps ==="
"${PIP[@]}" "mkdocs>=1.5" "mkdocs-material>=9" "mkdocstrings[python]>=0.24" \
    "pymdown-extensions" fpdf2 pypdf

echo "=== Compile check ==="
python -m py_compile src/refactor_utils/*.py autoall/src/autoall/__init__.py && echo "compile OK"
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
