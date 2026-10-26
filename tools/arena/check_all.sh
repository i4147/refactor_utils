#!/usr/bin/env bash
# check_all.sh — Full CI as used in session: tests + docs + examples + self-consolidation
# Usage: ./tools/arena/check_all.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

echo "********** SETUP **********"
./tools/arena/setup.sh

echo "********** PYLINT / COMPILE CHECK **********"
python -m py_compile src/refactor_utils/*.py && echo "compile OK"

echo "********** TESTS **********"
./tools/arena/run_tests.sh
pytest -q --tb=short 2>&1 | tail -20

echo "********** DOCS **********"
./tools/arena/build_docs.sh

echo "********** EXAMPLES **********"
./tools/arena/run_examples.sh 2>&1 | tail -30

echo "********** SELF-CONSOLIDATION **********"
./tools/arena/consolidate_self.sh

echo "********** FLAG COVERAGE **********"
./tools/arena/consolidate_examples.sh 2>&1 | tail -30

echo "✔ ALL CHECKS PASSED"
