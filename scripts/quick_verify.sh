#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/venv311/bin/python}"

cd "$PROJECT_ROOT"

echo "=== RECURDO QUICK VERIFY ==="
echo ""

echo "1. Compile check..."
"$PYTHON_BIN" -m py_compile \
  backend/main.py \
  backend/retrieval/search.py \
  backend/retrieval/query_parser.py \
  backend/cognition/reranker.py \
  backend/graph/store.py \
  backend/graph/candidates.py
echo "   ok"

echo ""
echo "2. Core tests..."
"$PYTHON_BIN" -m unittest \
  backend.test_search_ranking \
  backend.test_query_intent \
  backend.test_cognitive_reranker \
  backend.test_search_resilience \
  backend.test_health_keepalive \
  2>&1 | tail -5

echo ""
echo "3. Live health check..."
if "$PYTHON_BIN" - <<'PY'
from backend.benchmark_real import detect_running_backend_base
raise SystemExit(0 if detect_running_backend_base() else 1)
PY
then
  bash "$SCRIPT_DIR/health_check.sh"
else
  echo "   backend not running - skip live check"
fi

echo ""
echo "=== QUICK VERIFY DONE ==="
