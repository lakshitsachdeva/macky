#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MODEL_DIR="$SCRIPT_DIR/models/nomic-embed-text-v1.5"
SPEC_DIR="$SCRIPT_DIR/build/spec"

if [ -n "${PYTHON_BIN:-}" ]; then
  PYTHON="$PYTHON_BIN"
elif [ -x "$ROOT_DIR/venv311/bin/python" ]; then
  PYTHON="$ROOT_DIR/venv311/bin/python"
elif [ -x "$ROOT_DIR/venv/bin/python" ]; then
  PYTHON="$ROOT_DIR/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  PYTHON="$(command -v python)"
fi

cd "$SCRIPT_DIR"

if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
  echo "PyInstaller not found. Create a venv and install build deps first." >&2
  exit 1
fi

if [ ! -f "$MODEL_DIR/config.json" ]; then
  echo "Bundled embedding model missing at $MODEL_DIR"
  echo "Run: $PYTHON $ROOT_DIR/scripts/pre_bundle_model.py"
  exit 1
fi

rm -rf build dist
mkdir -p "$SPEC_DIR"

PYI_ARGS=(
  --onedir
  --name recurdo-backend
  --paths "$SCRIPT_DIR"
  --specpath "$SPEC_DIR"
  --add-data "$MODEL_DIR:models/nomic-embed-text-v1.5"
  --hidden-import=uvicorn.logging
  --hidden-import=uvicorn.loops
  --hidden-import=uvicorn.loops.auto
  --hidden-import=uvicorn.protocols
  --hidden-import=uvicorn.protocols.http
  --hidden-import=uvicorn.protocols.http.auto
  --hidden-import=uvicorn.protocols.websockets
  --hidden-import=uvicorn.protocols.websockets.auto
  --hidden-import=uvicorn.lifespan
  --hidden-import=uvicorn.lifespan.on
  --collect-all=uvicorn
  --hidden-import=faiss
  --hidden-import=fitz
  --hidden-import=docx
  --hidden-import=openpyxl
  --hidden-import=pptx
  --hidden-import=sentence_transformers
  --hidden-import=einops
  --collect-all=einops
  --hidden-import=pytesseract
  --hidden-import=google.oauth2.credentials
  --hidden-import=google.auth.transport.requests
  --hidden-import=google_auth_oauthlib.flow
  --hidden-import=googleapiclient.discovery
  --collect-all=google.generativeai
  --collect-all=google.ai.generativelanguage_v1beta
  --collect-all=google.ai.generativelanguage
  --collect-all=google.api_core
  --hidden-import=google.generativeai
  --hidden-import=google.generativeai.types
  --hidden-import=google.ai.generativelanguage_v1beta
  --hidden-import=google.ai.generativelanguage
  --hidden-import=googleapiclient.errors
  --hidden-import=googleapiclient.http
  --copy-metadata=sentence-transformers
  --exclude-module=pandas
  --exclude-module=pyarrow
  --exclude-module=datasets
  --exclude-module=hf_xet
  --exclude-module=tensorboard
  --exclude-module=torchvision
  --exclude-module=google.generativeai
)

"$PYTHON" -m PyInstaller "${PYI_ARGS[@]}" main.py

echo "Backend bundle built at dist/recurdo-backend"
