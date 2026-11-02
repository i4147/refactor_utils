#!/usr/bin/env bash
# setup.sh — Install refactor_utils (editable) + runtime/test/docs deps.
# Usage: ./arena/setup.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== Installing refactor_utils (editable) ==="
pip install -e . --break-system-packages --quiet

echo "=== Installing runtime/test extras ==="
pip install --break-system-packages --quiet pytest pytest-cov tqdm

echo "=== Installing docs extras ==="
pip install --break-system-packages --quiet "mkdocs>=1.4" "mkdocs-material>=9" "mkdocstrings[python]>=0.5" fpdf2 pypdf

echo "=== Installing autoall (sibling package used for __all__) ==="
pip install -e autoall --break-system-packages --quiet

echo "=== Compile check ==="
python -m py_compile src/refactor_utils/*.py autoall/src/autoall/__init__.py && echo "compile OK"

echo "✔ setup complete — try: ./arena/run_tests.sh"
