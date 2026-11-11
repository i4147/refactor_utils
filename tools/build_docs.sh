#!/usr/bin/env bash
# Build the HTML documentation into site/ and regenerate docs.pdf.
#
#   tools/build_docs.sh             HTML + PDF
#   tools/build_docs.sh --html      HTML only
#   tools/build_docs.sh --pdf       PDF only
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$ROOT"

html=1
pdf=1
case "${1:-}" in
    --html) pdf=0 ;;
    --pdf) html=0 ;;
    "") ;;
    -h|--help) sed -n '2,6p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac

if [ "$html" = 1 ]; then
    need_module mkdocs "pip install -e '.[docs]'"
    "$PY" -m mkdocs build --strict
    echo "HTML site: $ROOT/site/index.html"
fi
if [ "$pdf" = 1 ]; then
    need_module xhtml2pdf "pip install -e '.[docs]'"
    "$PY" tools/build_pdf.py
fi
