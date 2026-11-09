#!/usr/bin/env bash
# session_commands.sh — Raw bash commands executed during the arena session (for audit)
# This file is a verbatim log of the helper invocations we used.
# Each block was originally run via the `bash` tool in the sandbox.

# ---------------------------------------------------------------------------
# Initial exploration
# ---------------------------------------------------------------------------
# cat prompt.md
# ls -la src/refactor_utils
# cat src/refactor_utils/iwgfyrkugf.py | head -500
# git log --oneline --graph --all
# git show main:mk_new_repo.sh

# ---------------------------------------------------------------------------
# Setup & verification of prompt.md implementation (first session)
# ---------------------------------------------------------------------------
# pip install -e . --break-system-packages
# pip install loguru tree_sitter tree_sitter_python --break-system-packages
# pyrefactor --help
# pip install pytest mkdocs mkdocs-material mkdocstrings[python] --break-system-packages

# ---------------------------------------------------------------------------
# Manual consolidation demos (second session, before tests)
# ---------------------------------------------------------------------------
# mkdir -p /tmp/testproj/pkg/sub
# cat > /tmp/testproj/pkg/a.py <<'PY' ... (sample_project equivalent)
# pyrefactor /tmp/testproj/pkg -o /tmp/out_test --dry-run --log-level INFO
# pyrefactor /tmp/testproj/pkg -o /tmp/out_test --overwrite --log-level INFO
# pyrefactor /tmp/testproj/pkg -o /tmp/out_test --stdout --log-level ERROR | head -80
# pyrefactor /tmp/testproj/pkg -o /tmp/out_test --diff --log-level INFO
# pyrefactor /tmp/testproj/pkg -o /tmp/out_test --format --overwrite
# pyrefactor /tmp/testproj/pkg -o /tmp/out_test --strip-docstrings --strip-comments --source-map --preserve-modules
# pyrefactor /tmp/testproj/pkg -o /tmp/out_test --rename-strategy suffix
# pyrefactor /tmp/testproj/pkg -o /tmp/out_test --dedupe aggressive
# pyrefactor /tmp/testproj/pkg -o /tmp/out_test --all respect
# pyrefactor /tmp/testproj/pkg -o /tmp/out_flat --mode flat
# pyrefactor /tmp/testproj/pkg -o /tmp/out_split --mode split
# pyrefactor /tmp/testproj/pkg -o /tmp/out_test --validate
# pyrefactor /tmp/testproj/pkg -o /tmp/out_test --jobs 1 / --no-parallel
# pyrefactor /tmp/testproj/pkg -o /tmp/out_test --check
# echo "# extra" >> /tmp/testproj/pkg/a.py ; pyrefactor ... --check ; pyrefactor ... --diff
# cat > /tmp/testproj/pkg/bad.py <<'PY' def oops( ... ; pyrefactor ... --allow-syntax-errors
# pyrefactor ... --fail-on-warning ; --max-skipped ; --include/--exclude ; --config ; --main-name ; --generate-guard

# ---------------------------------------------------------------------------
# Test suite creation
# ---------------------------------------------------------------------------
# pip install pytest mkdocs ... (above)
# mkdir -p tests examples/data_files_example examples/config_example docs/api
# cat > tests/conftest.py <<'PY' ...
# cat > tests/test_discovery.py ...
# ... (all test files created via write_file tool)
# pytest -q
# pytest -v --collect-only
# mkdocs build --strict
# python examples/basic_usage.py
# python examples/advanced_flags.py
# python examples/programmatic_api.py
# pyrefactor examples/data_files_example/mypackage -o /tmp/example_data_out --copy-data --validate
# git add -A ; git commit -m "Add pytest tests..." ; git push origin arena/73f433aa-refactor-utils

# ---------------------------------------------------------------------------
# Self-consolidation verification
# ---------------------------------------------------------------------------
# pyrefactor src/refactor_utils -o /tmp/self_out --overwrite --log-level INFO --validate
# ls -R /tmp/self_out
# python -c "import py_compile; py_compile.compile('/tmp/self_out/_consolidated.py', doraise=True)"
# pyrefactor --help | grep -E "dry-run|stdout|config|..."

# ---------------------------------------------------------------------------
# Git/PR ops
# ---------------------------------------------------------------------------
# git status ; git log --oneline -5
# gh auth status
# gh pr create --title "Add pytest tests, examples, and mkdocs docs" --body "..." --base main
# gh pr view 1 --json state,mergedAt
# git fetch origin ; git log origin/main --oneline -5

# ---------------------------------------------------------------------------
# This file reconstructs those steps as executable checks
# ---------------------------------------------------------------------------
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
echo "Replaying session commands (dry-run)..."
./tools/arena/setup.sh 2>&1 | tail -5
./tools/arena/run_tests.sh 2>&1 | tail -5
./tools/arena/build_docs.sh 2>&1 | tail -5
./tools/arena/consolidate_self.sh 2>&1 | tail -10
echo "✔ session_commands.sh replay OK"
