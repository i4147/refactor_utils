#!/usr/bin/env bash
# build_docs.sh — Build the mkdocs HTML site as used in the arena session.
# Usage: ./arena/build_docs.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH=src

echo "=== Building mkdocs site ==="
mkdocs build 2>&1 | tail -8
echo "=== Site built to site/ ==="
ls -lh site/index.html

echo
echo "Note: mkdocs build --strict is aborted by a pre-existing warning"
echo "(root README.md conflicts with docs/index.md). Build above is non-strict."
echo "✔ Docs build complete — open site/index.html"
