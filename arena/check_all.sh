#!/usr/bin/env bash
# check_all.sh — Full arena-session CI: tests + docs + PDF + demo consolidation.
# Usage: ./arena/check_all.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "********** TESTS **********"
./arena/run_tests.sh

echo "********** DOCS (HTML) **********"
./arena/build_docs.sh

echo "********** PDF **********"
python arena/build_pdf.py

echo "********** DEMO CONSOLIDATION **********"
./arena/demo_consolidate.sh

echo "********** ARENA HELPERS **********"
python arena/helpers.py

echo "✔ ALL CHECKS PASSED"
