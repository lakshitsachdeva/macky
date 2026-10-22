#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SNAPSHOT_ROOT="${RECURDO_SNAPSHOT_ROOT:-$HOME/recurdo-restore-points}"
DATA_DIR="${RECURDO_DATA_DIR:-$HOME/.recurdo}"
APP_PATH="${RECURDO_APP_PATH:-/Applications/Recurdo.app}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/restore_point.sh create [name] [--skip-app] [--keep-running]
  ./scripts/restore_point.sh restore <latest|snapshot-path|snapshot-name> [--yes] [--skip-app] [--skip-code] [--no-relaunch]
  ./scripts/restore_point.sh list
  ./scripts/restore_point.sh show <latest|snapshot-path|snapshot-name>

Defaults:
  - Snapshots are stored in ~/recurdo-restore-points
  - Live app state is copied from ~/.recurdo
  - The installed /Applications/Recurdo.app bundle is included by default

Examples:
  ./scripts/restore_point.sh create "before-unified-master"
  ./scripts/restore_point.sh restore latest --yes
EOF
}

log() {
  printf '[restore-point] %s\n' "$*"
}

die() {
  printf '[restore-point] %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

timestamp() {
  date +%Y%m%d-%H%M%S
}

utc_timestamp() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

slugify() {
  printf '%s' "${1:-snapshot}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-'
}

resolve_snapshot_dir() {
  local ref="${1:-latest}"
  local candidate=""

  if [[ "$ref" == "latest" ]]; then
    candidate="$SNAPSHOT_ROOT/latest"
  elif [[ -d "$ref" ]]; then
    candidate="$ref"
  elif [[ -d "$SNAPSHOT_ROOT/$ref" ]]; then
    candidate="$SNAPSHOT_ROOT/$ref"
  else
    die "Snapshot not found: $ref"
  fi

  if [[ ! -d "$candidate" ]]; then
    die "Snapshot directory does not exist: $candidate"
  fi

  cd "$candidate" && pwd
}

manifest_get() {
  local manifest_path="$1"
  local key="$2"
  sed -n "s/^${key}=//p" "$manifest_path" | head -n 1
}

is_recurdo_running() {
  pgrep -f '/Applications/Recurdo.app/Contents/MacOS/recurdo-everywhere' >/dev/null 2>&1 \
    || pgrep -f '/Applications/Recurdo.app/Contents/Resources/backend/recurdo-backend/recurdo-backend' >/dev/null 2>&1
}

quit_recurdo_if_running() {
  local was_running=0

  if is_recurdo_running; then
    was_running=1
    log "Closing Recurdo for a consistent snapshot"
    osascript -e 'tell application "Recurdo" to quit' >/dev/null 2>&1 || true

    for _ in $(seq 1 40); do
      if ! is_recurdo_running; then
        break
      fi
      sleep 1
    done
  fi

  if is_recurdo_running; then
    die "Recurdo is still running. Close it and try again."
  fi

  printf '%s\n' "$was_running"
}

relaunch_recurdo() {
  if [[ -d "$APP_PATH" ]]; then
    open -a Recurdo
  fi
}

capture_repo_state() {
  local repo_dir="$1"
  local overlay_root="$2"
  local overlay_list_file="$3"
  local deleted_list_file="$4"

  {
    git -C "$repo_dir" diff --diff-filter=ACMRTUXB --name-only HEAD
    git -C "$repo_dir" ls-files --others --exclude-standard
  } | awk 'NF && !seen[$0]++' > "$overlay_list_file"

  git -C "$repo_dir" diff --diff-filter=D --name-only HEAD | awk 'NF && !seen[$0]++' > "$deleted_list_file"

  while IFS= read -r relpath; do
    [[ -z "$relpath" ]] && continue
    [[ -e "$repo_dir/$relpath" ]] || continue
    mkdir -p "$overlay_root/$(dirname "$relpath")"
    cp -p "$repo_dir/$relpath" "$overlay_root/$relpath"
  done < "$overlay_list_file"

  if [[ -f "$repo_dir/.env" ]]; then
    cp -p "$repo_dir/.env" "$overlay_root/.env"
  fi
}

write_manifest() {
  local manifest_path="$1"
  local snapshot_name="$2"
  local repo_branch="$3"
  local repo_commit="$4"
  local repo_remote="$5"
  local app_was_running="$6"

  cat > "$manifest_path" <<EOF
snapshot_name=$snapshot_name
created_at=$(utc_timestamp)
repo_dir=$REPO_DIR
repo_branch=$repo_branch
repo_commit=$repo_commit
repo_remote=$repo_remote
data_source=$DATA_DIR
app_source=$APP_PATH
app_was_running=$app_was_running
EOF
}

write_snapshot_wrappers() {
  local snapshot_dir="$1"

  cp -p "$0" "$snapshot_dir/restore_snapshot.sh"
  cat > "$snapshot_dir/Restore.command" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/restore_snapshot.sh" restore "$SCRIPT_DIR" --yes "$@"
EOF
  chmod +x "$snapshot_dir/restore_snapshot.sh" "$snapshot_dir/Restore.command"
}

create_snapshot() {
  require_cmd git
  require_cmd rsync
  require_cmd sqlite3
  require_cmd osascript

  local label="manual"
  local include_app=1
  local keep_running=0

  if [[ $# -gt 0 && "${1:-}" != --* ]]; then
    label="$1"
    shift
  fi

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --skip-app)
        include_app=0
        ;;
      --keep-running)
        keep_running=1
        ;;
      *)
        die "Unknown create option: $1"
        ;;
    esac
    shift
  done

  mkdir -p "$SNAPSHOT_ROOT"

  local snapshot_name="recurdo-$(slugify "$label")-$(timestamp)"
  local snapshot_dir="$SNAPSHOT_ROOT/$snapshot_name"
  local app_was_running=0

  if [[ "$keep_running" -eq 0 ]]; then
    app_was_running="$(quit_recurdo_if_running)"
  fi

  log "Creating snapshot at $snapshot_dir"
  mkdir -p "$snapshot_dir/data" "$snapshot_dir/repo/overlays" "$snapshot_dir/meta"
  rsync -a "$DATA_DIR/" "$snapshot_dir/data/recurdo-home/"

  if [[ "$include_app" -eq 1 && -d "$APP_PATH" ]]; then
    require_cmd ditto
    log "Copying installed Recurdo.app bundle"
    mkdir -p "$snapshot_dir/app"
    ditto "$APP_PATH" "$snapshot_dir/app/Recurdo.app"
  fi

  local repo_commit
  local repo_branch
  local repo_remote
  repo_commit="$(git -C "$REPO_DIR" rev-parse HEAD)"
  repo_branch="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"
  repo_remote="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"

  git -C "$REPO_DIR" status --short --branch > "$snapshot_dir/meta/git-status.txt"
  git -C "$REPO_DIR" diff > "$snapshot_dir/meta/git-diff.patch"
  git -C "$REPO_DIR" bundle create "$snapshot_dir/repo/repo.bundle" --all >/dev/null
  capture_repo_state \
    "$REPO_DIR" \
    "$snapshot_dir/repo/overlays" \
    "$snapshot_dir/meta/overlay-files.txt" \
    "$snapshot_dir/meta/tracked-deletions.txt"

  if [[ -f "$snapshot_dir/data/recurdo-home/memory.db" ]]; then
    sqlite3 "$snapshot_dir/data/recurdo-home/memory.db" \
      "select 'items', count(*) from items union all select 'active_items', count(*) from items where coalesce(is_excluded,0)=0 union all select 'watched_files', count(*) from watched_files union all select 'pending_watched_files', count(*) from watched_files where status='pending';" \
      > "$snapshot_dir/meta/live-counts.txt"
  fi

  du -sh "$snapshot_dir" > "$snapshot_dir/meta/snapshot-size.txt"
  write_manifest "$snapshot_dir/meta/manifest.txt" "$snapshot_name" "$repo_branch" "$repo_commit" "$repo_remote" "$app_was_running"
  write_snapshot_wrappers "$snapshot_dir"

  ln -sfn "$snapshot_dir" "$SNAPSHOT_ROOT/latest"

  if [[ "$app_was_running" -eq 1 ]]; then
    relaunch_recurdo
  fi

  log "Snapshot created: $snapshot_dir"
}

restore_snapshot() {
  require_cmd git
  require_cmd rsync
  require_cmd osascript

  local ref="${1:-latest}"
  local yes=0
  local restore_app=1
  local restore_code=1
  local relaunch=1
  shift || true

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes)
        yes=1
        ;;
      --skip-app)
        restore_app=0
        ;;
      --skip-code)
        restore_code=0
        ;;
      --no-relaunch)
        relaunch=0
        ;;
      *)
        die "Unknown restore option: $1"
        ;;
    esac
    shift
  done

  local snapshot_dir
  snapshot_dir="$(resolve_snapshot_dir "$ref")"
  local manifest_path="$snapshot_dir/meta/manifest.txt"
  [[ -f "$manifest_path" ]] || die "Missing manifest: $manifest_path"

  local repo_dir
  local repo_branch
  local repo_commit
  repo_dir="$(manifest_get "$manifest_path" repo_dir)"
  repo_branch="$(manifest_get "$manifest_path" repo_branch)"
  repo_commit="$(manifest_get "$manifest_path" repo_commit)"

  if [[ "$yes" -ne 1 ]]; then
    die "Restore is destructive. Re-run with --yes when you're ready."
  fi

  local app_was_running
  app_was_running="$(quit_recurdo_if_running)"

  log "Restoring live data from $snapshot_dir"
  rm -rf "$DATA_DIR"
  mkdir -p "$DATA_DIR"
  rsync -a "$snapshot_dir/data/recurdo-home/" "$DATA_DIR/"

  if [[ "$restore_app" -eq 1 && -d "$snapshot_dir/app/Recurdo.app" ]]; then
    require_cmd ditto
    log "Restoring installed Recurdo.app bundle"
    rm -rf "$APP_PATH"
    ditto "$snapshot_dir/app/Recurdo.app" "$APP_PATH"
  fi

  if [[ "$restore_code" -eq 1 ]]; then
    [[ -d "$repo_dir/.git" ]] || die "Repo not found at $repo_dir"
    log "Restoring repo state in $repo_dir"
    git -C "$repo_dir" checkout -B "$repo_branch" "$repo_commit"
    git -C "$repo_dir" reset --hard "$repo_commit"
    git -C "$repo_dir" clean -fd
    rsync -a "$snapshot_dir/repo/overlays/" "$repo_dir/"
    if [[ -f "$snapshot_dir/meta/tracked-deletions.txt" ]]; then
      while IFS= read -r relpath; do
        [[ -z "$relpath" ]] && continue
        rm -rf "$repo_dir/$relpath"
      done < "$snapshot_dir/meta/tracked-deletions.txt"
    fi
  fi

  if [[ "$relaunch" -eq 1 || "$app_was_running" -eq 1 ]]; then
    relaunch_recurdo
  fi

  log "Restore complete"
}

list_snapshots() {
  mkdir -p "$SNAPSHOT_ROOT"
  find "$SNAPSHOT_ROOT" -maxdepth 1 -mindepth 1 -type d -name 'recurdo-*' | sort
}

show_snapshot() {
  local snapshot_dir
  snapshot_dir="$(resolve_snapshot_dir "${1:-latest}")"
  sed -n '1,120p' "$snapshot_dir/meta/manifest.txt"
  if [[ -f "$snapshot_dir/meta/live-counts.txt" ]]; then
    printf '\n'
    cat "$snapshot_dir/meta/live-counts.txt"
  fi
  if [[ -f "$snapshot_dir/meta/snapshot-size.txt" ]]; then
    printf '\n'
    cat "$snapshot_dir/meta/snapshot-size.txt"
  fi
}

main() {
  local command="${1:-}"
  [[ -n "$command" ]] || {
    usage
    exit 1
  }
  shift || true

  case "$command" in
    create)
      create_snapshot "$@"
      ;;
    restore)
      restore_snapshot "$@"
      ;;
    list)
      list_snapshots
      ;;
    show)
      show_snapshot "$@"
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage
      die "Unknown command: $command"
      ;;
  esac
}

main "$@"
