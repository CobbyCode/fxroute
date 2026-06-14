#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -Eeuo pipefail

MODE="update"
REPO_PATH="${FXROUTE_REPO_PATH:-}"
RESTART_MODE="auto"
CONFIG_FILE="${FXROUTE_INSTALL_CONFIG:-$HOME/.config/fxroute/install-config.env}"
SERVICE_NAME="${FXROUTE_SERVICE_NAME:-fxroute}"

usage() {
  cat <<EOF
Usage: scripts/update_fxroute.sh [options]

Options:
  --check             Fetch and report local/remote status without changing files
  --restore           Reset the checkout to origin/main and return to a clean public release
  --repo <path>       Override the FXRoute git checkout path
  --no-restart        Do not restart the FXRoute service
  --defer-restart     Leave restart to the caller after this script exits
  -h, --help          Show this help
EOF
}

log() { printf '[fxroute-update] %s\n' "$*"; }
die() { printf '[fxroute-update][error] %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      MODE="check"
      shift
      ;;
    --restore)
      MODE="restore"
      shift
      ;;
    --repo)
      [[ $# -ge 2 ]] || die "--repo requires a path"
      REPO_PATH="$2"
      shift 2
      ;;
    --no-restart)
      RESTART_MODE="none"
      shift
      ;;
    --defer-restart)
      RESTART_MODE="defer"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

read_install_config() {
  [[ -f "$CONFIG_FILE" ]] || return 0
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  if [[ -z "$REPO_PATH" ]]; then
    REPO_PATH="${FXROUTE_INSTALL_ROOT:-${INSTALL_ROOT:-}}"
  fi
  SERVICE_NAME="${FXROUTE_SERVICE_NAME:-${SERVICE_NAME}}"
}

script_repo_path() {
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd
}

resolve_repo_path() {
  read_install_config
  if [[ -z "$REPO_PATH" ]]; then
    REPO_PATH="$(script_repo_path)"
  fi
  python3 - <<'PY' "$REPO_PATH"
import os, sys
print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
}

git_remote_ref() {
  local upstream=""
  upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
  if [[ -n "$upstream" ]]; then
    printf '%s\n' "$upstream"
    return 0
  fi
  if git show-ref --verify --quiet refs/remotes/origin/main; then
    printf 'origin/main\n'
    return 0
  fi
  if git show-ref --verify --quiet refs/remotes/origin/master; then
    printf 'origin/master\n'
    return 0
  fi
  upstream="$(git branch -r --format='%(refname:short)' | grep -E '^origin/[^/]+$' | head -n 1 || true)"
  [[ -n "$upstream" ]] || die "No upstream or origin branch found"
  printf '%s\n' "$upstream"
}

git_short() {
  git rev-parse --short=12 "$1"
}

version_at() {
  local ref="$1"
  if [[ "$ref" == "HEAD" && -f VERSION ]]; then
    tr -d '[:space:]' < VERSION
    return 0
  fi
  git show "${ref}:VERSION" 2>/dev/null | tr -d '[:space:]' || true
}

requirements_hash() {
  sha256sum requirements.txt | awk '{print $1}'
}

install_dependencies_if_needed() {
  local venv_dir="$REPO_PATH/.venv"
  local marker="$venv_dir/.fxroute-requirements.sha256"
  local current_hash=""
  local installed_hash=""

  [[ -f "$REPO_PATH/requirements.txt" ]] || {
    log "No requirements.txt found; dependency step skipped."
    return 0
  }

  current_hash="$(requirements_hash)"
  [[ -f "$marker" ]] && installed_hash="$(tr -d '[:space:]' < "$marker")"
  if [[ -x "$venv_dir/bin/python3" && "$installed_hash" == "$current_hash" ]]; then
    log "Python dependencies unchanged; install step skipped."
    return 0
  fi

  log "Installing Python dependencies in $venv_dir"
  python3 -m venv "$venv_dir"
  "$venv_dir/bin/python3" -m pip install --upgrade pip setuptools wheel
  "$venv_dir/bin/pip" install -r "$REPO_PATH/requirements.txt"
  printf '%s\n' "$current_hash" > "$marker"
  log "Python dependencies are up to date."
}

run_production_build() {
  if [[ -f package.json ]]; then
    log "Running frontend production build."
    if [[ -f package-lock.json ]]; then
      npm ci
    else
      npm install
    fi
    npm run build
    return 0
  fi

  log "No package.json found; FXRoute has no separate frontend build step."
  log "Validating Python files instead."
  "$REPO_PATH/.venv/bin/python3" -m py_compile main.py config.py measurement.py
}

build_pipewire_stage1_if_needed() {
  local build_script="$REPO_PATH/pipewire_stage1/build.sh"
  local binary="$REPO_PATH/pipewire_stage1/build/fxroute_21_passthrough"

  [[ -f "$build_script" ]] || return 0

  if [[ -f "$binary" ]]; then
    if [[ "$build_script" -nt "$binary" ]]; then
      log "PipeWire 2.1 helper source changed; rebuilding."
    else
      log "PipeWire 2.1 helper binary is up to date."
      return 0
    fi
  else
    log "PipeWire 2.1 helper binary missing; building."
  fi

  if ! bash "$build_script"; then
    log "PipeWire 2.1 helper build failed — 2.1 output mode will not be available until this is resolved."
  else
    log "PipeWire 2.1 helper built successfully."
  fi
}

restart_service_if_needed() {
  case "$RESTART_MODE" in
    none)
      log "Service restart skipped by caller."
      return 0
      ;;
    defer)
      log "Service restart deferred to caller."
      return 0
      ;;
  esac

  if ! command -v systemctl >/dev/null 2>&1; then
    log "systemctl not available; restart skipped."
    return 0
  fi

  if systemctl --user list-unit-files "${SERVICE_NAME}.service" --no-legend 2>/dev/null | grep -q "^${SERVICE_NAME}\.service"; then
    log "Restarting user service ${SERVICE_NAME}.service"
    systemctl --user daemon-reload || true
    systemctl --user restart "${SERVICE_NAME}.service"
    log "Restart completed: ${SERVICE_NAME}.service"
  else
    log "User service ${SERVICE_NAME}.service is not installed; restart skipped."
  fi
}

setup_repo() {
  command -v git >/dev/null 2>&1 || die "git is required"
  command -v python3 >/dev/null 2>&1 || die "python3 is required"

  REPO_PATH="$(resolve_repo_path)"
  [[ -d "$REPO_PATH" ]] || die "FXRoute repo path does not exist: $REPO_PATH"
  cd "$REPO_PATH"

  [[ -f main.py && -f requirements.txt ]] || die "Path does not look like FXRoute: $REPO_PATH"
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "FXRoute is not running from a git checkout: $REPO_PATH"

  local current_version current_commit
  current_version="$(version_at HEAD)"
  current_commit="$(git_short HEAD)"

  log "Repo path: $REPO_PATH"
  log "Service name: $SERVICE_NAME"
  log "Current: ${current_version:-unknown} (${current_commit})"
}

main() {
  setup_repo

  local current_version current_commit remote_ref remote_version remote_commit
  current_version="$(version_at HEAD)"
  current_commit="$(git_short HEAD)"

  local dirty=""
  dirty="$(git status --porcelain=v1 --untracked-files=all)"
  if [[ -n "$dirty" ]]; then
    local tracked_changes="" untracked_only=""
    tracked_changes="$(printf '%s\n' "$dirty" | grep -vE '^\?\? ' || true)"
    untracked_only="$(printf '%s\n' "$dirty" | grep -E '^\?\? ' || true)"
    printf '[fxroute-update][error] Local changes detected. Update blocked to protect this checkout.\n' >&2
    if [[ -n "$tracked_changes" ]]; then
      printf '[fxroute-update][error] Modified source files (must be resolved before update):\n' >&2
      printf '%s\n' "$tracked_changes" >&2
    fi
    if [[ -n "$untracked_only" ]]; then
      printf '[fxroute-update][error] Untracked files present (may be runtime artifacts; add to .gitignore or remove):\n' >&2
      printf '%s\n' "$untracked_only" >&2
    fi
    printf '[fxroute-update][error] Commit, remove, or intentionally handle these files before updating.\n' >&2
    exit 1
  fi

  log "Fetching GitHub updates."
  git fetch --prune --no-tags

  remote_ref="$(git_remote_ref)"
  remote_version="$(version_at "$remote_ref")"
  remote_commit="$(git_short "$remote_ref")"

  log "Current: ${current_version:-unknown} (${current_commit})"
  log "Remote:  ${remote_version:-unknown} (${remote_commit}) from ${remote_ref}"

  if [[ "$current_commit" == "$remote_commit" ]]; then
    log "FXRoute is already up to date."
    return 0
  fi

  if git merge-base --is-ancestor "$remote_ref" HEAD; then
    log "Local checkout is newer than GitHub; waiting for the matching upload."
    log "FXRoute is already up to date."
    return 0
  fi

  if [[ "$MODE" == "check" ]]; then
    log "Update available."
    return 0
  fi

  if ! git merge-base --is-ancestor HEAD "$remote_ref"; then
    die "Remote branch is not a fast-forward from the current checkout. Update blocked."
  fi

  log "Pulling updates with fast-forward only."
  git pull --ff-only

  install_dependencies_if_needed
  run_production_build
  build_pipewire_stage1_if_needed
  restart_service_if_needed

  current_version="$(version_at HEAD)"
  current_commit="$(git_short HEAD)"
  log "Update completed: ${current_version:-unknown} (${current_commit})"
}

restore_main() {
  setup_repo

  local remote_ref remote_version remote_commit
  log "Restore: fetching GitHub updates."
  git fetch --prune --no-tags

  remote_ref="$(git_remote_ref)"
  remote_version="$(version_at "$remote_ref")"
  remote_commit="$(git_short "$remote_ref")"

  log "Restore: target: ${remote_version:-unknown} (${remote_commit}) from ${remote_ref}"

  local dirty=""
  dirty="$(git status --porcelain=v1 --untracked-files=all)"
  if [[ -n "$dirty" ]]; then
    backup_dir="$REPO_PATH/backups"
    mkdir -p "$backup_dir"
    patch_file="$backup_dir/local-changes-$(date -u +%Y%m%d-%H%M%S).patch"
    log "Restore: saving local changes to $patch_file"
    git diff HEAD -- > "$patch_file" 2>/dev/null || true
    if [[ -s "$patch_file" ]]; then
      log "Restore: tracked source changes saved as patch."
    else
      rm -f "$patch_file"
      log "Restore: no tracked source changes to save."
    fi
    log "Restore: discarding local changes and resetting to ${remote_ref}."
    git reset --hard "$remote_ref"
    log "Restore: cleaning untracked files (excluding runtime cache and config)."
    git clean -fd -e media/cache -e .env -e .env.local -e .venv -e backups -e BUILD_ID
  else
    log "Restore: working tree is already clean; resetting to ${remote_ref}."
    git reset --hard "$remote_ref"
  fi

  install_dependencies_if_needed
  run_production_build
  build_pipewire_stage1_if_needed
  restart_service_if_needed

  local restored_version restored_commit
  restored_version="$(version_at HEAD)"
  restored_commit="$(git_short HEAD)"
  log "Restore completed: ${restored_version:-unknown} (${restored_commit})"
  return 0
}

case "$MODE" in
  restore)
    restore_main
    ;;
  *)
    main "$@"
    ;;
esac
