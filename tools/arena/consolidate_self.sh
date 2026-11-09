#!/usr/bin/env bash
# consolidate_self.sh — Self-consolidation test used during session
# Verifies data-file preservation, validate, compile on the project itself
# Usage: ./tools/arena/consolidate_self.sh [output_dir]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

OUT="${1:-/tmp/self_out}"
echo "=== Self-consolidation: src/refactor_utils -> $OUT ==="
pip install -e . --break-system-packages --quiet 2>&1 | tail || true

rm -rf "$OUT"
pyrefactor src/refactor_utils -o "$OUT" --overwrite --log-level INFO --validate 2>&1 | tail -30

echo "=== Output ==="
ls -R "$OUT" 2>&1 | head -n 100

echo "=== Data files copied ==="
find "$OUT" -name "*.json" -ls 2>&1 | head
ls -lh "$OUT"/refactor_utils/data/ 2>&1 | head

echo "=== Compile check ==="
python -c "import py_compile; py_compile.compile('$OUT/_consolidated.py', doraise=True); print('compile OK')"

echo "=== Help flags check ==="
pyrefactor --help | grep -E "dry-run|stdout|config|format|diff|overwrite|include|rename-strategy|dedupe|imports|all |namespace|source-map|fail-on|jobs|validate|mode" | head -20

echo "=== Single mode preserved ==="
rm -rf /tmp/self_out2
pyrefactor src/refactor_utils -o /tmp/self_out2 --mode single --overwrite --log-level ERROR
ls /tmp/self_out2/_consolidated.py && echo "single mode OK"

echo "✔ Self-consolidation complete"
