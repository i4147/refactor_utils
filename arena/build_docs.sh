#!/usr/bin/env bash
# build_docs.sh — build the mkdocs HTML site (strict) as used in this session.
#
# The build is expected to be warning-free: docs/README.md is excluded via
# `exclude_docs` in mkdocs.yml so it cannot clash with docs/index.md.
#
# Usage: ./arena/build_docs.sh [--serve]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -d .venv ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi
export PYTHONPATH="src:${PYTHONPATH:-}"

if [[ "${1:-}" == "--serve" ]]; then
    echo "=== Serving docs on http://127.0.0.1:8000 ==="
    exec mkdocs serve --dev-addr 0.0.0.0:8000
fi

echo "=== mkdocs build --strict ==="
mkdocs build --strict
echo "=== Site built ==="
ls -lh site/index.html
echo "✔ open site/index.html (or run: ./arena/build_docs.sh --serve)"
