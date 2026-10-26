#!/usr/bin/env bash
# run_examples.sh — Execute all example scripts as done in session
# Usage: ./tools/arena/run_examples.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

pip install -e . --break-system-packages --quiet 2>&1 | tail || true

echo "=== basic_usage.py ==="
python examples/basic_usage.py 2>&1 | head -n 100

echo "=== advanced_flags.py ==="
python examples/advanced_flags.py 2>&1 | head -n 200

echo "=== programmatic_api.py ==="
python examples/programmatic_api.py 2>&1 | head -n 200

echo "=== data_files_example CLI: --copy-data --validate ==="
pyrefactor examples/data_files_example/mypackage -o /tmp/example_data_out --copy-data --validate --overwrite --log-level INFO 2>&1 | tail -20
ls -R /tmp/example_data_out 2>&1 | head -n 30

echo "=== config_example CLI: --config ==="
pyrefactor examples/data_files_example/mypackage -o /tmp/example_config_out --config examples/config_example/pyproject.toml --overwrite --log-level INFO 2>&1 | tail -20

echo "✔ Examples complete"
