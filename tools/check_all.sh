#!/usr/bin/env bash
# check_all.sh — Full arena-session CI: tests + docs + PDF + demo consolidation.
# check_all.sh — full session CI: tests + contract checks + docs + PDF + demos.
#
# Usage: ./arena/check_all.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -d .venv ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi
export PYTHONPATH="src:${PYTHONPATH:-}"

echo "********** TESTS **********"
./arena/run_tests.sh

echo "********** PROMPT CONTRACT **********"
python arena/verify_prompt.py

echo "********** DOCSTRINGS **********"
python arena/docstring_audit.py
python arena/docstring_audit.py --tests

echo "********** DOCS (HTML) **********"
./arena/build_docs.sh

echo "********** PDF **********"
python arena/build_pdf.py

echo "********** DEMO CONSOLIDATION **********"
./arena/demo_consolidate.sh
echo "********** REAL PACKAGES **********"
./arena/test_on_packages.sh

echo "********** ARENA HELPERS **********"
python arena/helpers.py
echo "********** EXAMPLE DEMOS **********"
./arena/demo_consolidate.sh

echo "✔ ALL CHECKS PASSED"
