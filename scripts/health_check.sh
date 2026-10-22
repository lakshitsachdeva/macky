#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/venv311/bin/python}"
SESSION_PATH="${SESSION_PATH:-$HOME/.recurdo/session.json}"
FAST_QUERY="${FAST_QUERY:-resume lakshit}"
FAST_QUERY_TARGET_MS="${FAST_QUERY_TARGET_MS:-2000}"

cd "$PROJECT_ROOT"

detect_base_url() {
  "$PYTHON_BIN" - <<'PY'
from backend.benchmark_real import detect_running_backend_base
base = detect_running_backend_base()
if base:
    print(base)
PY
}

read_token() {
  if [ ! -f "$SESSION_PATH" ]; then
    return 1
  fi
  "$PYTHON_BIN" - <<'PY' "$SESSION_PATH"
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
token = str(payload.get("token") or "").strip()
if token:
    print(token)
else:
    raise SystemExit(1)
PY
}

BASE_URL="${BASE_URL:-}"
if [ -z "$BASE_URL" ]; then
  BASE_URL="$(detect_base_url)"
fi
if [ -z "$BASE_URL" ]; then
  echo "No running backend detected"
  exit 1
fi
echo "== Recurdo Health Check =="
echo "Backend: $BASE_URL"

HEALTH_JSON="$(curl -fsS "$BASE_URL/health")"
"$PYTHON_BIN" - <<'PY' "$HEALTH_JSON"
import json, sys
payload = json.loads(sys.argv[1])
status = payload.get("status", "unknown")
if status != "ok":
    raise SystemExit(f"health status is {status}")
print(f"status={status}")
print(f"unique_memories={payload.get('unique_memories', 0)}")
print(f"files_watched={payload.get('files_watched', 0)}")
print(f"db_items={payload.get('db_items', 0)}")
print(f"faiss_items={payload.get('faiss_items', 0)}")
print(f"graph_ready={payload.get('graph', {}).get('ready', False)}")
print(f"cognitive_ready={payload.get('cognitive', {}).get('ready', False)}")
print(f"model_loaded={payload.get('model_loaded', False)}")
print(f"keepalive_last_error={payload.get('keepalive', {}).get('last_error')}")
PY

if TOKEN="$(read_token 2>/dev/null)"; then
  SEARCH_TMP="$(mktemp)"
  SEARCH_MS="$(curl -sS -o "$SEARCH_TMP" -w '%{time_total}' \
    -X POST "$BASE_URL/search?explain=true" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"query\":\"$FAST_QUERY\",\"limit\":3}")"
  "$PYTHON_BIN" - <<'PY' "$SEARCH_TMP" "$SEARCH_MS" "$FAST_QUERY_TARGET_MS" "$FAST_QUERY"
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
elapsed_ms = float(sys.argv[2]) * 1000.0
target_ms = float(sys.argv[3])
query = sys.argv[4]
if not isinstance(payload, list):
    raise SystemExit("search response was not a list")
print(f"fast_query={query}")
print(f"fast_query_elapsed_ms={elapsed_ms:.1f}")
print(f"fast_query_results={len(payload)}")
if elapsed_ms > target_ms:
    raise SystemExit(f"fast query exceeded target ({elapsed_ms:.1f}ms > {target_ms:.1f}ms)")
PY
  rm -f "$SEARCH_TMP"
else
  echo "session token not found; skipped authenticated search probe"
fi

echo "health check passed"
