#!/usr/bin/env bash
# Run every project check: tests, sample output, docs build.
#
#   tools/run_checks.sh             tests + sample + docs (strict)
#   tools/run_checks.sh --lint      also run ruff (reports, does not fail the run)
#   tools/run_checks.sh --no-docs   skip the docs build
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$ROOT"

lint=0
docs=1
for arg in "$@"; do
    case "$arg" in
        --lint) lint=1 ;;
        --no-docs) docs=0 ;;
        -h|--help) sed -n '2,6p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

echo "== tests"
need_module pytest "pip install -e '.[dev]'"
"$PY" -m pytest tests -q

echo "== sample (mytest/result.py is up to date and valid)"
"$PY" tools/regen_sample.py --check
"$PY" tools/check_syntax.py mytest src

if [ "$docs" = 1 ]; then
    echo "== docs (strict build)"
    need_module mkdocs "pip install -e '.[docs]'"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    "$PY" -m mkdocs build --strict --quiet --site-dir "$tmp/site"
fi

if [ "$lint" = 1 ]; then
    echo "== ruff (informational)"
    "$PY" -m ruff check src tests tools || true
fi

echo "== all checks passed"
