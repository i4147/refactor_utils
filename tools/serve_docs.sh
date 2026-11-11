#!/usr/bin/env bash
# Live preview of the documentation.
#
#   tools/serve_docs.sh [ADDRESS]     default 0.0.0.0:8000
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$ROOT"
need_module mkdocs "pip install -e '.[docs]'"
exec "$PY" -m mkdocs serve --dev-addr "${1:-0.0.0.0:8000}"
