#!/usr/bin/env bash
# build_docs.sh — Build mkdocs site (strict) as done in arena session
# Usage: ./tools/arena/build_docs.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

echo "=== Building mkdocs (strict) ==="
mkdocs build --strict 2>&1 | tail -30
echo "=== Site built to site/ ==="
ls -lh site/ 2>&1 | head -20
echo "=== Docs nav check ==="
cat mkdocs.yml | head -n 40
echo "✔ Docs build complete"
