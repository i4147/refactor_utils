#!/usr/bin/env bash
# test_on_packages.sh — run pyrefactor over real packages and show the results.
#
# Exercises the tool against:
#   * this repository's own package (src/refactor_utils, pyproject name detected)
#   * the vendored autoall package
#   * the example packages in examples/
#   * a couple of installed third-party packages (when available)
#
# Nothing is written inside the repository: every run uses `-o` into a temp
# directory. For each package the script reports the detected name, number of
# modules, output size, whether the result compiles, and whether a second
# `--check` run is clean (idempotency).
#
# Usage: ./arena/test_on_packages.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src:${PYTHONPATH:-}"

PY="${PYTHON:-python}"
WORK="$(mktemp -d -t pyrefactor-pkgs-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null || true)"

declare -a TARGETS=(
  "refactor_utils|$ROOT/src/refactor_utils"
  "autoall|$ROOT/autoall/src/autoall"
  "mypackage|$ROOT/examples/data_files_example/mypackage"
)
for thirdparty in loguru fpdf jinja2 markdown; do
  if [[ -n "$SITE" && -d "$SITE/$thirdparty" ]]; then
    TARGETS+=("$thirdparty|$SITE/$thirdparty")
  fi
done

printf '%-16s %-18s %7s %9s %9s %8s %s\n' \
  "TARGET" "PKG NAME" "MODULES" "SIZE" "SECONDS" "COMPILES" "CHECK"
printf '%.0s-' {1..90}; echo

failures=0
for entry in "${TARGETS[@]}"; do
  label="${entry%%|*}"
  source="${entry#*|}"
  out="$WORK/$label"
  log="$WORK/$label.log"

  start=$(date +%s%N)
  if ! "$PY" -m refactor_utils.cli "$source" -o "$out" --log-level INFO >"$log" 2>&1; then
    printf '%-16s %-18s %7s %9s %9s %8s %s\n' "$label" "-" "-" "-" "-" "NO" "ERROR"
    tail -3 "$log" | sed 's/^/    /'
    failures=$((failures + 1))
    continue
  fi
  end=$(date +%s%N)
  seconds=$(awk -v s="$start" -v e="$end" 'BEGIN{printf "%.2f", (e-s)/1e9}')

  pkgname="$(basename "$out"/*.py .py)"
  modules="$(grep -o 'Parsing [0-9]* files' "$log" | awk 'NR==1{print $2}' || true)"
  modules="${modules:-?}"
  size="$(du -h "$out/$pkgname.py" | cut -f1)"

  if "$PY" -m py_compile "$out/$pkgname.py" 2>>"$log"; then compiles="YES"; else compiles="NO"; fi
  if "$PY" -m refactor_utils.cli "$source" -o "$out" --check --log-level ERROR >>"$log" 2>&1; then
    check="CLEAN"
  else
    check="DRIFT"
    failures=$((failures + 1))
  fi

  printf '%-16s %-18s %7s %9s %9s %8s %s\n' \
    "$label" "$pkgname" "$modules" "$size" "${seconds}s" "$compiles" "$check"
done

echo
echo "Generated files live in $WORK (removed on exit)."
if [[ $failures -gt 0 ]]; then
  echo "FAILED: $failures target(s) had problems"
  exit 1
fi
echo "✔ $(( ${#TARGETS[@]} )) package(s) consolidated, compiled and checked cleanly"
