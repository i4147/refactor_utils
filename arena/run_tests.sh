#!/usr/bin/env bash
# run_tests.sh — Run the pytest suite used during the arena session.
# Usage: ./arena/run_tests.sh [--cov|--verbose]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH=src

echo "=== Collecting tests ==="
pytest --collect-only -q 2>&1 | tail -5

echo "=== Running pytest ==="
if [[ "${1:-}" == "--cov" ]]; then
    pytest -v --cov=refactor_utils --cov-report=term-missing
elif [[ "${1:-}" == "--verbose" ]]; then
    pytest -v
else
    pytest -q
fi

echo "=== New consolidated-flow tests ==="
pytest tests/test_consolidated.py -v 2>&1 | tail -25

echo "✔ Tests complete"
