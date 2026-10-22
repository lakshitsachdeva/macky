#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

./venv311/bin/pip install pyinstaller

./venv311/bin/pyinstaller \
  --noconfirm \
  --clean \
  --onefile \
  --name backend \
  --add-data "backend:backend" \
  backend/tauri_sidecar.py

HOST_TRIPLE="$(/opt/homebrew/opt/rustup/bin/rustc -Vv | awk '/host:/ {print $2}')"
mkdir -p frontend/src-tauri/binaries
cp "dist/backend" "frontend/src-tauri/binaries/backend-${HOST_TRIPLE}"

echo "Copied backend sidecar to frontend/src-tauri/binaries/backend-${HOST_TRIPLE}"
