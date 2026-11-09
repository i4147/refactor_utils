#!/usr/bin/env bash
# serve_docs.sh — Serve mkdocs locally (port 8000) as in arena session
# Usage: ./tools/arena/serve_docs.sh
# Preview host: https://{port}-{sandboxId}.e2b.app — bind 0.0.0.0, allow origin
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

echo "=== Serving mkdocs on 0.0.0.0:8000 ==="
echo "Preview: https://8000-\${E2B_SANDBOX_ID}.e2b.app (proxied)"
mkdocs serve -a 0.0.0.0:8000
