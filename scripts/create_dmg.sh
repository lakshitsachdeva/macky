#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
TAURI_DIR="$FRONTEND_DIR/src-tauri"
TARGET_DIR="$TAURI_DIR/target"
APP_BUNDLE_DIR="$TARGET_DIR/release/bundle/macos"
APP_BUNDLE_PATH="$APP_BUNDLE_DIR/Recurdo.app"
DMG_OUTPUT_DIR="$TARGET_DIR/release/bundle/dmg"
DMG_OUTPUT_PATH="$DMG_OUTPUT_DIR/Recurdo_0.1.0_aarch64.dmg"

cleanup_recurdo_mounts() {
  local found=0

  while IFS=$'\t' read -r device mount_point; do
    if [ -z "${device:-}" ] || [ -z "${mount_point:-}" ]; then
      continue
    fi

    found=1
    echo "Detaching stale Recurdo volume at $mount_point..."
    hdiutil detach "$device" >/dev/null 2>&1 || hdiutil detach -force "$device" >/dev/null 2>&1 || true
  done < <(
    mount | awk '
      $3 ~ /^\/Volumes\/Recurdo( [0-9]+)?$/ {
        print $1 "\t" $3
      }
    '
  )

  if [ "$found" -eq 1 ]; then
    sleep 1
  fi
}

ensure_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

resolve_create_dmg() {
  if command -v create-dmg >/dev/null 2>&1; then
    printf 'create-dmg'
    return 0
  fi
  if command -v npx >/dev/null 2>&1; then
    printf 'npx --yes create-dmg'
    return 0
  fi
  return 1
}

print_disk_budget() {
  local free_kb free_gb
  free_kb="$(df -k /System/Volumes/Data | awk 'NR==2 {print $4}')"
  free_gb="$(python3 - <<'PY' "$free_kb"
import sys
free_kb = int(sys.argv[1])
print(f"{free_kb / 1024 / 1024:.2f}")
PY
)"
  echo "Free disk space: ${free_gb} GiB"
  echo "Expected working room for .app + DMG staging: ~3.0-4.5 GiB"
  if python3 - <<'PY' "$free_kb"
import sys
free_kb = int(sys.argv[1])
sys.exit(0 if free_kb >= 3 * 1024 * 1024 else 1)
PY
  then
    :
  else
    echo "Warning: free space is below 3 GiB. The build may fail unless more space is freed first." >&2
  fi
}

cleanup_recurdo_mounts
print_disk_budget

CREATE_DMG_CMD="$(resolve_create_dmg)" || {
  echo "Missing required command: create-dmg (or npx fallback)" >&2
  exit 1
}

echo "Building backend bundle..."
bash "$BACKEND_DIR/build_backend.sh"

if [ -n "${SUPABASE_URL:-}" ]; then
  export VITE_SUPABASE_URL="$SUPABASE_URL"
fi

if [ -n "${SUPABASE_ANON_KEY:-}" ]; then
  export VITE_SUPABASE_ANON_KEY="$SUPABASE_ANON_KEY"
fi

cd "$FRONTEND_DIR"

echo "Building release binary without Tauri bundling..."
CI=true npm run tauri build -- --no-bundle --ci

echo "Bundling macOS .app only..."
CI=true npm run tauri bundle -- --bundles app --ci

if [ ! -d "$APP_BUNDLE_PATH" ]; then
  echo "Expected app bundle not found at $APP_BUNDLE_PATH" >&2
  exit 1
fi

mkdir -p "$DMG_OUTPUT_DIR"
rm -f "$DMG_OUTPUT_PATH"

echo "Creating DMG via create-dmg..."
eval "$CREATE_DMG_CMD" \
  --skip-jenkins \
  --no-internet-enable \
  --volname "Recurdo" \
  --window-size 660 400 \
  --icon-size 128 \
  --icon "Recurdo.app" 180 170 \
  --app-drop-link 480 170 \
  "$DMG_OUTPUT_PATH" \
  "$APP_BUNDLE_DIR"

echo ""
echo "Output: $DMG_OUTPUT_PATH"
