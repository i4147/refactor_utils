#!/usr/bin/env bash
# consolidate_examples.sh — Consolidate example projects with various flags (session coverage)
# Usage: ./tools/arena/consolidate_examples.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

pip install -e . --break-system-packages --quiet 2>&1 | tail || true

run_demo() {
  local name="$1"; shift
  echo "=== $name: $* ==="
  pyrefactor "$@" --log-level INFO 2>&1 | tail -20
  echo "exit=$?"
}

# Create a temp demo pkg similar to conftest.py sample_project
TMP_PKG=$(mktemp -d)
mkdir -p "$TMP_PKG/pkg/data" "$TMP_PKG/pkg/sub"
cat > "$TMP_PKG/pkg/__init__.py" <<'PY'
from .a import foo
from .b import Bar
__all__ = ["foo", "Bar"]
PY
cat > "$TMP_PKG/pkg/a.py" <<'PY'
"""module a"""
import os
from .b import Bar as B2
X = 1
def foo(x=X):
    """docstring"""
    # comment
    return x + 1
if __name__ == "__main__":
    print(foo())
PY
cat > "$TMP_PKG/pkg/b.py" <<'PY'
"""module b"""
import json
Y = 2
class Bar:
    def method(self): return Y
PY
cat > "$TMP_PKG/pkg/c.py" <<'PY'
X = 1
def foo(): return 99
PY
echo '{"key":"value"}' > "$TMP_PKG/pkg/data/sample.json"
echo 'a,b' > "$TMP_PKG/pkg/data/sample.csv"
echo 'info: test' > "$TMP_PKG/pkg/config.yaml"

run_demo "dry-run" "$TMP_PKG/pkg" -o /tmp/demo_out --dry-run
run_demo "overwrite" "$TMP_PKG/pkg" -o /tmp/demo_out --overwrite
run_demo "stdout" "$TMP_PKG/pkg" -o /tmp/demo_out --stdout --log-level ERROR 2>&1 | head -20
run_demo "diff" "$TMP_PKG/pkg" -o /tmp/demo_out --diff
run_demo "format" "$TMP_PKG/pkg" -o /tmp/demo_out --format --overwrite
run_demo "strip-docstrings+source-map" "$TMP_PKG/pkg" -o /tmp/demo_out --strip-docstrings --strip-comments --source-map --preserve-modules --overwrite
run_demo "rename suffix" "$TMP_PKG/pkg" -o /tmp/demo_out --rename-strategy suffix --overwrite
run_demo "dedupe none vs aggressive" "$TMP_PKG/pkg" -o /tmp/demo_out --dedupe none --overwrite
run_demo "all respect" "$TMP_PKG/pkg" -o /tmp/demo_out --all respect --overwrite
run_demo "mode flat" "$TMP_PKG/pkg" -o /tmp/demo_flat --mode flat --overwrite
run_demo "mode split" "$TMP_PKG/pkg" -o /tmp/demo_split --mode split --overwrite
run_demo "validate" "$TMP_PKG/pkg" -o /tmp/demo_out --validate --overwrite
run_demo "jobs no-parallel" "$TMP_PKG/pkg" -o /tmp/demo_out --jobs 1 --overwrite
run_demo "jobs no-parallel2" "$TMP_PKG/pkg" -o /tmp/demo_out --no-parallel --overwrite
run_demo "check up-to-date" "$TMP_PKG/pkg" -o /tmp/demo_out --check
run_demo "allow-syntax-errors" "$TMP_PKG/pkg" -o /tmp/demo_out --allow-syntax-errors --overwrite

echo "=== include/exclude ==="
mkdir -p "$TMP_PKG/inc/pkg"
echo "X=1" > "$TMP_PKG/inc/pkg/keep.py"
echo "Y=2" > "$TMP_PKG/inc/pkg/conftest.py"
pyrefactor "$TMP_PKG/inc/pkg" -o /tmp/inc_out --overwrite --log-level INFO 2>&1 | tail -5
pyrefactor "$TMP_PKG/inc/pkg" -o /tmp/inc_out --include "conftest.py" --overwrite --log-level INFO 2>&1 | tail -5

echo "=== pyproject config ==="
cat > "$TMP_PKG/pyproject.toml" <<'PY'
[tool.pyrefactor]
exclude = ["c.py"]
rename_strategy = "suffix"
all = "respect"
PY
pyrefactor "$TMP_PKG/pkg" -o /tmp/demo_out --config "$TMP_PKG/pyproject.toml" --overwrite --log-level INFO 2>&1 | tail -10

rm -rf "$TMP_PKG"
echo "✔ All flag demos complete"
