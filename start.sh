#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"

# Custody is intentionally self-contained on the SSD while it is being
# brought back to life.  Explicit paths win over any stale paths in a copied
# .env file, so the index, graph, auth cache, and attachments stay beside the
# source tree instead of silently filling the internal disk.
export RECURDO_ENV=development
export RECURDO_LOCAL_MODE=1
export RECURDO_ALLOW_LOCAL_DEV_AUTH=1
export RECURDO_SHARE_EMBEDDING_MODEL=1
# Do not let a copied legacy library start indexing every persisted watched
# folder before the search UI is responsive. Explicit folder selection or the
# manual index trigger still starts indexing.
export RECURDO_AUTO_INDEX_ON_START=0
export RECURDO_DATA_DIR="$ROOT_DIR/data"
export RECURDO_ENV_FILE="$ROOT_DIR/.env"
export WATCHED_DIRS=""
export GEMINI_API_KEY=""
export GEMINI_VISION_ENABLED=0
export RECURDO_ENABLE_GEMINI_VISION=0
export SUPABASE_URL=""
export SUPABASE_ANON_KEY=""
export SUPABASE_SERVICE_ROLE_KEY=""
export VITE_SUPABASE_URL=""
export VITE_SUPABASE_ANON_KEY=""
export DB_PATH="$ROOT_DIR/data/memory.db"
export TEXT_FAISS_PATH="$ROOT_DIR/data/text_faiss.index"
export VECTORS_DB_PATH="$ROOT_DIR/data/vectors.db"
export GRAPH_DB_PATH="$ROOT_DIR/data/graph.db"
export COGNITIVE_DB_PATH="$ROOT_DIR/data/cognitive_layer.db"
export AUTH_CACHE_PATH="$ROOT_DIR/data/auth_cache.json"
export ATTACHMENTS_DIR="$ROOT_DIR/data/attachments"
export GMAIL_TOKENS_DIR="$ROOT_DIR/data/gmail_tokens"
export VITE_RECURDO_LOCAL_MODE=1

mkdir -p "$RUN_DIR" "$ROOT_DIR/data"

if [[ ! -f "$ROOT_DIR/.env" && -f "$ROOT_DIR/.env.example" ]]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
fi

start_backend() {
  if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Backend already running on http://localhost:8000"
    return
  fi

  local python_bin="${RECURDO_PYTHON:-}"
  if [[ -z "$python_bin" ]]; then
    for candidate in \
      "$ROOT_DIR/.venv-custody/bin/python" \
      "$ROOT_DIR/venv311/bin/python" \
      "$(command -v python3.11 2>/dev/null || true)" \
      "$(command -v python3 2>/dev/null || true)"; do
      if [[ -n "$candidate" && -x "$candidate" ]]; then
        python_bin="$candidate"
        break
      fi
    done
  fi

  if [[ -z "$python_bin" || ! -x "$python_bin" ]]; then
    echo "No usable Python 3.11+ environment found."
    echo "Create one on the SSD with: /opt/homebrew/bin/python3.11 -m venv .venv-custody"
    echo "Then install backend deps with: ./.venv-custody/bin/pip install -r backend/requirements.txt"
    exit 1
  fi

  echo "Starting backend..."
  (
    cd "$ROOT_DIR"
    nohup "$python_bin" -m uvicorn backend.main:app --port 8000 \
      >"$BACKEND_LOG" 2>&1 &
    echo $! >"$RUN_DIR/backend.pid"
  )
}

start_frontend() {
  if lsof -nP -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Frontend already running on http://localhost:5173"
    return
  fi

  if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
    echo "Missing frontend/node_modules"
    echo "Run: cd frontend && npm install"
    exit 1
  fi

  echo "Starting frontend..."
  (
    cd "$ROOT_DIR/frontend"
    VITE_RECURDO_LOCAL_MODE=1 nohup npm run dev -- --host 127.0.0.1 >"$FRONTEND_LOG" 2>&1 &
    echo $! >"$RUN_DIR/frontend.pid"
  )
}

wait_for_http() {
  local url="$1"
  local name="$2"
  local attempts="${3:-30}"

  for ((i = 0; i < attempts; i++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "$name ready: $url"
      return 0
    fi
    sleep 1
  done

  echo "$name did not become ready: $url"
  return 1
}

start_backend
start_frontend

wait_for_http "http://localhost:8000/health" "Backend"
wait_for_http "http://localhost:5173" "Frontend"

echo
echo "Custody is up in SSD-first local mode."
echo "Backend log:  $BACKEND_LOG"
echo "Frontend log: $FRONTEND_LOG"
echo "Search UI:    http://localhost:5173"
