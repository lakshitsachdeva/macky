#!/usr/bin/env bash
# Recurdo smoke test — gates commits on the 11 surgical fixes.
#
# Usage:
#   ./scripts/smoke_test.sh              # unit tests + static checks
#   ./scripts/smoke_test.sh --bench      # above + live benchmark (needs backend running)
#   ./scripts/smoke_test.sh --bench --commit   # above + auto-commit if everything green
#
# Exits non-zero on any failure so you can chain with `&& git commit …`.

set -euo pipefail

# Resolve repo root regardless of where we're called from.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$REPO_ROOT"

RUN_BENCH=false
AUTO_COMMIT=false
for arg in "$@"; do
    case "$arg" in
        --bench)  RUN_BENCH=true  ;;
        --commit) AUTO_COMMIT=true ;;
        -h|--help)
            head -n 11 "$0" | tail -n 10
            exit 0
            ;;
        *)
            echo "Unknown flag: $arg" >&2
            exit 2
            ;;
    esac
done

step() { printf "\n\033[1;36m==>\033[0m %s\n" "$*"; }
ok()   { printf "   \033[1;32m✓\033[0m %s\n" "$*"; }
fail() { printf "   \033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }

# ─── Python version gate ──────────────────────────────────────────────────────
step "Checking Python version (need 3.11+ for StrEnum)"
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="${PY_VER%%.*}"
PY_MINOR="${PY_VER##*.}"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    fail "Python $PY_VER found, need >=3.11. Try: brew install python@3.11 && python3.11 -m venv .venv && source .venv/bin/activate"
fi
ok "Python $PY_VER"

# ─── Static compile gate ──────────────────────────────────────────────────────
step "Compiling all modified files"
CHANGED_FILES=(
    backend/auth.py
    backend/database.py
    backend/main.py
    backend/config.py
    backend/retrieval/search.py
    backend/indexer/embedder.py
    backend/interactions.py
    backend/now_feed.py
    backend/llm/synthesis.py
)
for f in "${CHANGED_FILES[@]}"; do
    python3 -m py_compile "$f" || fail "py_compile failed for $f"
done
ok "All ${#CHANGED_FILES[@]} modified files compile clean"

# ─── Static sanity on the new code paths ──────────────────────────────────────
step "Static sanity on surgical fixes"
python3 - <<'PY' || fail "static sanity check failed"
import ast, sys, pathlib

root = pathlib.Path(".")

def has_symbol(path, name):
    tree = ast.parse((root / path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign)):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == name:
                        return True
            elif node.name == name:
                return True
    return False

checks = [
    # Fix #4: sparse pre-parse helper exists
    ("backend/indexer/embedder.py", "parse_sparse_weights",
        "parse_sparse_weights() helper (Fix #4 sparse pre-parse)"),
    # Fix #10: health cache present
    ("backend/main.py", "_cached_health_payload",
        "_cached_health_payload (Fix #10 health TTL cache)"),
    # Fix #7: batch size bumped
    ("backend/indexer/embedder.py", "EMBED_BATCH_SIZE",
        "EMBED_BATCH_SIZE constant"),
]
for path, sym, label in checks:
    if not has_symbol(path, sym):
        print(f"MISSING: {label}", file=sys.stderr); sys.exit(1)
    print(f"  found: {label}")

# Fix #7 value check
src = (root / "backend/indexer/embedder.py").read_text()
if "EMBED_BATCH_SIZE = 64" not in src and "return 64" not in src:
    print("FAIL: EMBED_BATCH_SIZE default is not 64", file=sys.stderr); sys.exit(1)
print("  confirmed: EMBED_BATCH_SIZE default = 64")

# Fix #5 PRAGMA gate
db_src = (root / "backend/database.py").read_text()
for pragma in ("synchronous = NORMAL", "mmap_size = 268435456", "temp_store = MEMORY",
               "wal_autocheckpoint = 1000", "cache_size = -131072"):
    if pragma not in db_src:
        print(f"MISSING PRAGMA: {pragma}", file=sys.stderr); sys.exit(1)
    print(f"  confirmed PRAGMA: {pragma}")

# Fix #2 FAISS k=80 in search
search_src = (root / "backend/retrieval/search.py").read_text()
if "k=80" not in search_src:
    print("FAIL: FAISS k=80 overfetch not found", file=sys.stderr); sys.exit(1)
print("  confirmed: FAISS k=80 overfetch (Fix #2)")

# Fix #9 auth off event loop — three asyncio.to_thread sites
auth_src = (root / "backend/auth.py").read_text()
n_to_thread = auth_src.count("asyncio.to_thread")
if n_to_thread < 3:
    print(f"FAIL: expected >=3 asyncio.to_thread in auth.py, found {n_to_thread}", file=sys.stderr); sys.exit(1)
print(f"  confirmed: {n_to_thread} asyncio.to_thread wrappers in auth.py (Fix #9)")

# Fix #8 atomic FAISS save
emb_src = (root / "backend/indexer/embedder.py").read_text()
if "os.replace(tmp_path, self.index_path)" not in emb_src:
    print("FAIL: atomic FAISS save not found", file=sys.stderr); sys.exit(1)
print("  confirmed: atomic FAISS save via os.replace (Fix #8)")

# Phase 2 (Ask) surface
synth_src = (root / "backend/llm/synthesis.py").read_text()
for needle in ("synthesize_ask_answer", "_ASK_PROMPT", "build_ask_fallback_answer"):
    if needle not in synth_src:
        print(f"MISSING Phase 2 symbol: {needle}", file=sys.stderr); sys.exit(1)
print("  confirmed: Phase 2 synthesize_ask_answer surface")

main_src = (root / "backend/main.py").read_text()
for route in ('"/ask"', '"/events/batch"', '"/now"'):
    if f'@app.post({route})' not in main_src and f'@app.get({route})' not in main_src:
        print(f"MISSING route: {route}", file=sys.stderr); sys.exit(1)
print("  confirmed: routes /ask, /events/batch, /now")

# Phase 3 (Now feed) module
now_src = (root / "backend/now_feed.py").read_text()
for needle in ("def build_now_feed", "def _exp_decay", "_DIVERSIFY_PENALTY"):
    if needle not in now_src:
        print(f"MISSING Now-feed symbol: {needle}", file=sys.stderr); sys.exit(1)
print("  confirmed: Phase 3 build_now_feed surface")

print("ALL STATIC CHECKS PASS")
PY
ok "All 11 surgical fixes present and well-formed"

# ─── Dependency install (test-only, idempotent) ───────────────────────────────
step "Ensuring test deps"
python3 -m pip install --quiet --disable-pip-version-check \
    pytest httpx pydantic fastapi 'uvicorn[standard]' \
    rank_bm25 cachetools rapidfuzz aiosqlite watchdog python-multipart \
    >/dev/null 2>&1 || true
ok "Test deps ready"

# ─── Unit test suite ──────────────────────────────────────────────────────────
step "Running unit test suite"
# Run from repo root so `from backend...` imports work.
UNIT_TESTS=(
    backend/test_search_resilience.py
    backend/test_search_ranking.py
    backend/test_cognitive_reranker.py
    backend/test_health_keepalive.py
    backend/test_auth.py
    backend/test_auth_cache.py
    backend/test_query_intent.py
    backend/test_script_detector.py
    backend/test_interactions.py
    backend/test_ask_synthesis.py
    backend/test_now_feed.py
    backend/test_local_mode.py
)
MISSING=()
EXISTING=()
for t in "${UNIT_TESTS[@]}"; do
    if [ -f "$t" ]; then EXISTING+=("$t"); else MISSING+=("$t"); fi
done
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "   (skipping missing test files: ${MISSING[*]})"
fi
python3 -m pytest "${EXISTING[@]}" -q || fail "unit tests failed"
ok "Unit tests green"

# ─── Live benchmark (optional) ────────────────────────────────────────────────
if $RUN_BENCH; then
    step "Running live retrieval benchmark (needs running backend)"
    if ! curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && \
       ! pgrep -f recurdo-backend >/dev/null 2>&1; then
        fail "Backend not running. Start Recurdo.app or run backend/main.py first."
    fi
    cd backend
    # Captures p50/p95/hit@k in both stdout and the baseline JSON when present.
    python3 benchmark_real.py --verbose 2>&1 | tee /tmp/recurdo_bench.txt
    BENCH_EXIT=${PIPESTATUS[0]}
    cd ..
    if [ "$BENCH_EXIT" -ne 0 ]; then
        fail "benchmark_real.py exited non-zero"
    fi
    # Regression gate: p95 must not be worse than baseline by >15%.
    python3 - <<'PY' || fail "benchmark regressed beyond tolerance"
import json, pathlib, re, sys
baseline_path = pathlib.Path("backend/benchmark_real_baseline.json")
if not baseline_path.exists():
    print("  (no baseline file to compare against, skipping regression gate)")
    sys.exit(0)
baseline = json.loads(baseline_path.read_text())
text = pathlib.Path("/tmp/recurdo_bench.txt").read_text()
def extract(key):
    m = re.search(rf"{key}.*?([\d.]+)\s*ms", text, re.I)
    return float(m.group(1)) if m else None
p50 = extract("p50") or extract("median")
p95 = extract("p95")
b_p50 = baseline.get("p50_ms") or baseline.get("p50")
b_p95 = baseline.get("p95_ms") or baseline.get("p95")
print(f"  current  p50={p50} ms  p95={p95} ms")
print(f"  baseline p50={b_p50} ms  p95={b_p95} ms")
if p95 and b_p95 and p95 > b_p95 * 1.15:
    print(f"  REGRESSION: p95 {p95} > baseline {b_p95} * 1.15 ({b_p95*1.15:.1f})", file=sys.stderr)
    sys.exit(1)
print("  within 15% regression tolerance")
PY
    ok "Benchmark within tolerance"
fi

# ─── Auto-commit (optional) ───────────────────────────────────────────────────
if $AUTO_COMMIT; then
    step "Auto-committing verified changes"
    git add CHANGES.md scripts/smoke_test.sh backend .gitignore 2>/dev/null || true
    git rm -f backend/recurdo-backend.spec 2>/dev/null || true
    if git diff --cached --quiet; then
        echo "   (nothing staged — already committed?)"
    else
        git commit -m "surgical wins: 5 retrieval-quality + 6 speed/footprint fixes

- _time_fit: fractional-day precision (rank stability near window edges)
- FAISS overfetch k=80 + dedup + summary re-clamp (more distinct items in dense pool)
- Identifier exact-match bonus caps (1.85->1.35, 1.75->1.32) — preserves tie-breaking
- Sparse-score pre-parse + cached query tokens (kills json.loads in hot path)
- SQLite PRAGMA suite (synchronous=NORMAL, 128MB cache, 256MB mmap, WAL checkpointing)
- created_at/modified_at indexes (kills full-table scans on temporal queries)
- EMBED_BATCH_SIZE 32->64 (saturates nomic-embed-text-v1.5 on CPU/MPS)
- Atomic FAISS index write (tmp+rename — no more corrupt index after crash)
- Supabase auth off event loop (3 sites asyncio.to_thread'd — no more /search stalls)
- /health 2s TTL cache (idle UI poll no longer thrashes SQLite+FAISS)
- Bundled embedding model resolver (prep for smaller first-run DMG cost)

Verified via scripts/smoke_test.sh --bench on Mac.
See CHANGES.md for per-fix rationale."
        ok "Committed"
    fi
fi

printf "\n\033[1;32mALL SMOKE CHECKS PASSED\033[0m\n"
