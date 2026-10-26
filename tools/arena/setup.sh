#!/usr/bin/env bash
# setup.sh — Install project and dependencies used during arena session
# Usage: ./tools/arena/setup.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

echo "=== Setup: install project editable ==="
pip install -e . --break-system-packages 2>&1 | tail -20 || pip install -e . --break-system-packages --quiet

echo "=== Installing core dependencies ==="
pip install loguru tree-sitter tree_sitter_python --break-system-packages --quiet

echo "=== Installing extras: format, progress, test, docs ==="
pip install black ruff tqdm pytest pytest-cov --break-system-packages --quiet || true
pip install mkdocs mkdocs-material "mkdocstrings[python]" --break-system-packages --quiet || true

echo "=== Verifying ==="
pyrefactor --help | head -n 5
pyrefactor --version
pytest --version 2>&1 | head -1 || echo "pytest not installed"
mkdocs --version 2>&1 | head -1 || echo "mkdocs not installed"

echo "✔ Setup complete. You're on branch: $(git rev-parse --abbrev-ref HEAD)"
