#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
TARGET_RELEASE_DIR="$FRONTEND_DIR/src-tauri/target/release"
APP_VERSION="$(node -e "console.log(JSON.parse(require('fs').readFileSync('$FRONTEND_DIR/src-tauri/tauri.conf.json', 'utf8')).version)")"
APP_ARCH="$(uname -m | sed 's/^arm64$/aarch64/')"
RAW_DMG_PATH="$TARGET_RELEASE_DIR/bundle/macos/rw.Recurdo_${APP_VERSION}_${APP_ARCH}.dmg"
FINAL_DMG_DIR="$TARGET_RELEASE_DIR/bundle/dmg"
FINAL_DMG_PATH="$FINAL_DMG_DIR/Recurdo_${APP_VERSION}_${APP_ARCH}.dmg"
DEFAULT_UPDATER_KEY="$HOME/.tauri/recurdo-updater.key"
ROOT_ENV_FILE="$PROJECT_ROOT/.env"
LANDING_ENV_FILE="$PROJECT_ROOT/landing/.env.local"

load_env_file() {
  local env_file="$1"
  if [ -f "$env_file" ]; then
    echo "Loading release env: $env_file"
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi
}

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

strip_bundle_xattrs() {
  local bundle_path="$1"
  xattr -cr "$bundle_path" 2>/dev/null || true
  # Some tools attach FinderInfo/ResourceFork xattrs on nested files inside the .app.
  # codesign refuses these ("resource fork, Finder information, or similar detritus not allowed").
  xattr -d com.apple.FinderInfo "$bundle_path" 2>/dev/null || true
  xattr -rd com.apple.FinderInfo "$bundle_path" 2>/dev/null || true
  xattr -d com.apple.ResourceFork "$bundle_path" 2>/dev/null || true
  xattr -rd com.apple.ResourceFork "$bundle_path" 2>/dev/null || true
}

promote_raw_dmg_fallback() {
  if [ ! -f "$RAW_DMG_PATH" ]; then
    return 1
  fi

  cleanup_recurdo_mounts

  local attach_output device mount_point
  attach_output="$(hdiutil attach -readonly -nobrowse "$RAW_DMG_PATH" 2>/dev/null)" || return 1
  device="$(printf '%s\n' "$attach_output" | awk '/Apple_HFS/ {print $1; exit}')"
  mount_point="$(printf '%s\n' "$attach_output" | awk '/Apple_HFS/ {print $NF; exit}')"

  if [ -z "${mount_point:-}" ] || [ ! -d "$mount_point/Recurdo.app" ] || [ ! -L "$mount_point/Applications" ]; then
    if [ -n "${device:-}" ]; then
      hdiutil detach "$device" >/dev/null 2>&1 || hdiutil detach -force "$device" >/dev/null 2>&1 || true
    fi
    return 1
  fi

  if [ -n "${device:-}" ]; then
    hdiutil detach "$device" >/dev/null 2>&1 || hdiutil detach -force "$device" >/dev/null 2>&1 || true
  fi

  mkdir -p "$FINAL_DMG_DIR"
  rm -f "$FINAL_DMG_PATH"
  mv "$RAW_DMG_PATH" "$FINAL_DMG_PATH"
  echo ""
  echo "Promoted raw DMG fallback: $FINAL_DMG_PATH"
  echo "Note: this fallback DMG is writable (UDRW) but installable."
  return 0
}

postprocess_release_artifacts() {
  local app_path="$TARGET_RELEASE_DIR/bundle/macos/Recurdo.app"
  local update_tar="$TARGET_RELEASE_DIR/bundle/macos/Recurdo.app.tar.gz"
  local update_sig="$update_tar.sig"
  local signing_identity="${APPLE_SIGNING_IDENTITY:--}"

  if [ ! -d "$app_path" ]; then
    echo "App bundle not found at $app_path" >&2
    return 1
  fi

  # Avoid duplicating the full .app into /tmp (beta bundles can be multiple GB,
  # and that copy routinely fails on dev machines with limited disk space).
  # We instead strip xattrs + sign in-place and build the updater tarball from
  # the bundle output directly.
  strip_bundle_xattrs "$app_path"
  rm -rf "$app_path/Contents/_CodeSignature" 2>/dev/null || true
  codesign --force --deep --sign "$signing_identity" "$app_path"
  if ! codesign --verify --deep --strict --verbose=2 "$app_path"; then
    echo "Warning: codesign verify failed for $app_path (likely stray xattrs)." >&2
  fi

  (cd "$TARGET_RELEASE_DIR/bundle/macos" && COPYFILE_DISABLE=1 tar --no-mac-metadata -czf "$update_tar" Recurdo.app)

  if [ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
    local sign_args=()
    if [ -f "$TAURI_SIGNING_PRIVATE_KEY" ]; then
      sign_args=(-f "$TAURI_SIGNING_PRIVATE_KEY")
    else
      sign_args=(-k "$TAURI_SIGNING_PRIVATE_KEY")
    fi
    sign_args+=(-p "${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}")
    rm -f "$update_sig"
    (cd "$FRONTEND_DIR" && npm run tauri signer sign -- "${sign_args[@]}" "$update_tar")
  fi

  # Prefer the DMG produced by `tauri build` (it is already in the final
  # location under bundle/dmg/). Only fall back to promoting the raw UDRW DMG
  # if Tauri's bundler failed to move it.
  if [ ! -f "$FINAL_DMG_PATH" ] && ! promote_raw_dmg_fallback; then
    echo "Warning: DMG not found at $FINAL_DMG_PATH (and raw fallback missing)." >&2
  fi
}

echo "Building Recurdo release bundle..."

load_env_file "$ROOT_ENV_FILE"
load_env_file "$LANDING_ENV_FILE"

if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ] && [ -f "$DEFAULT_UPDATER_KEY" ]; then
  export TAURI_SIGNING_PRIVATE_KEY="$DEFAULT_UPDATER_KEY"
fi

if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
  echo "Warning: TAURI_SIGNING_PRIVATE_KEY is not set; updater artifacts will not be signed." >&2
fi

if ! security find-identity -v -p codesigning | grep -q "Developer ID Application"; then
  echo "Warning: no Developer ID Application signing identity found; this build will not be notarization-ready." >&2
fi

SUPABASE_BUILD_URL="${SUPABASE_URL:-${VITE_SUPABASE_URL:-}}"
SUPABASE_BUILD_ANON_KEY="${SUPABASE_ANON_KEY:-${VITE_SUPABASE_ANON_KEY:-}}"
if [ -z "$SUPABASE_BUILD_URL" ] || [ -z "$SUPABASE_BUILD_ANON_KEY" ]; then
  echo "Error: set SUPABASE_URL and SUPABASE_ANON_KEY before building a beta DMG." >&2
  echo "The source no longer contains hardcoded Supabase defaults." >&2
  exit 1
fi
export SUPABASE_URL="$SUPABASE_BUILD_URL"
export SUPABASE_ANON_KEY="$SUPABASE_BUILD_ANON_KEY"
export VITE_SUPABASE_URL="$SUPABASE_BUILD_URL"
export VITE_SUPABASE_ANON_KEY="$SUPABASE_BUILD_ANON_KEY"

cleanup_recurdo_mounts

cd "$BACKEND_DIR"
bash build_backend.sh
xattr -cr "$BACKEND_DIR/dist/recurdo-backend" "$FRONTEND_DIR/src-tauri/icons" 2>/dev/null || true

if [ -n "${SUPABASE_URL:-}" ]; then
  export VITE_SUPABASE_URL="$SUPABASE_URL"
fi

if [ -n "${SUPABASE_ANON_KEY:-}" ]; then
  export VITE_SUPABASE_ANON_KEY="$SUPABASE_ANON_KEY"
fi

cd "$FRONTEND_DIR"
if ! CI=true npm run tauri build -- --ci; then
  echo ""
  echo "Tauri DMG bundling failed; checking for raw DMG fallback..."
  if ! promote_raw_dmg_fallback; then
    exit 1
  fi
fi

postprocess_release_artifacts

echo ""
echo "Output: src-tauri/target/release/bundle/dmg/Recurdo_*.dmg"
echo "Test by installing the DMG on a machine without the repo."
