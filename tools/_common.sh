# Shared by the shell helpers in tools/. Source it, do not run it.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
else
    PY="${PYTHON:-python3}"
fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

need_module() {
    # need_module <import name> <hint>
    if ! "$PY" -c "import $1" 2>/dev/null; then
        echo "error: python module '$1' is missing. Install with: $2" >&2
        exit 1
    fi
}
