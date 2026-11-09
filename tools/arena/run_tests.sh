#!/usr/bin/env bash
# run_tests.sh — Run pytest suite used during arena session
# Usage: ./tools/arena/run_tests.sh [--cov|--verbose]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

pip install -e . --break-system-packages --quiet 2>&1 | tail || true

echo "=== Collecting tests ==="
pytest --collect-only -q 2>&1 | tail -20

echo "=== Running pytest ==="
if [[ "${1:-}" == "--cov" ]]; then
    pytest -v --cov=refactor_utils --cov-report=term-missing
elif [[ "${1:-}" == "--verbose" ]]; then
    pytest -v
else
    pytest -q
fi

echo "=== Quick discovery/parser/resolver/writer checks ==="
pytest tests/test_discovery.py tests/test_parser.py tests/test_resolver.py tests/test_writer.py -v 2>&1 | tail -30

echo "✔ Tests complete"
