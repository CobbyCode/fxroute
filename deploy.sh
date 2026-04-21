#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

REMOTE_HOST="${DEPLOY_HOST:-user@host}"
REMOTE_DIR="${DEPLOY_DIR:-/home/user/fxroute}"
REMOTE_SERVICE="${DEPLOY_SERVICE:-fxroute}"
RESTART_SERVICE=0
DRY_RUN=0
DELETE_REMOTE=0

usage() {
  cat <<EOF
Usage: ./deploy.sh [options]

Deploy FXRoute to the configured remote host with one root-level rsync.
This avoids the repeated split deploy problem where VERSION updates but static/ stays old.

Options:
  --restart       Restart the remote user service after sync
  --dry-run       Show what would be synced without changing remote files
  --delete        Also delete remote files that no longer exist locally
  --host <host>   Override remote SSH target (default: ${REMOTE_HOST})
  --dir <path>    Override remote app directory (default: ${REMOTE_DIR})
  --service <id>  Override remote user service name (default: ${REMOTE_SERVICE})
  -h, --help      Show this help

Env overrides:
  DEPLOY_HOST, DEPLOY_DIR, DEPLOY_SERVICE
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart)
      RESTART_SERVICE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --delete)
      DELETE_REMOTE=1
      shift
      ;;
    --host)
      REMOTE_HOST="$2"
      shift 2
      ;;
    --dir)
      REMOTE_DIR="$2"
      shift 2
      ;;
    --service)
      REMOTE_SERVICE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -f VERSION || ! -f static/index.html ]]; then
  echo "Run this script from the project root, or keep deploy.sh inside the project root." >&2
  exit 1
fi

VERSION="$(tr -d '[:space:]' < VERSION)"
if [[ -z "$VERSION" ]]; then
  echo "VERSION file is empty." >&2
  exit 1
fi

if ! grep -q "style.css?v=${VERSION}" static/index.html; then
  echo "static/index.html does not reference style.css?v=${VERSION}" >&2
  exit 1
fi
if ! grep -q "app.js?v=${VERSION}" static/index.html; then
  echo "static/index.html does not reference app.js?v=${VERSION}" >&2
  exit 1
fi

echo "==> Deploying FXRoute ${VERSION}"
echo "    Host:    ${REMOTE_HOST}"
echo "    Target:  ${REMOTE_DIR}"
[[ "$RESTART_SERVICE" -eq 1 ]] && echo "    Restart: ${REMOTE_SERVICE}"
[[ "$DRY_RUN" -eq 1 ]] && echo "    Mode:    dry-run"
[[ "$DELETE_REMOTE" -eq 1 ]] && echo "    Delete:  enabled"

RSYNC_ARGS=(
  -av
  --exclude=.git/
  --exclude=.venv/
  --exclude=__pycache__/
  --exclude=backups/
  --exclude=outputs/
  --exclude=inputs/
  --exclude=tickets/
  --exclude=media/raw/
  --exclude=media/reference/
  --exclude=scripts/prepare-public-export.sh
  --exclude=.mypy_cache/
  --exclude=.pytest_cache/
  --exclude='*.pyc'
  --exclude='.DS_Store'
  --exclude='ChatGPT Image*'
  --exclude='.env'
  --exclude='playlists.json'
  --exclude='stations.json'
)

[[ "$DELETE_REMOTE" -eq 1 ]] && RSYNC_ARGS+=(--delete)
[[ "$DRY_RUN" -eq 1 ]] && RSYNC_ARGS+=(--dry-run)

rsync "${RSYNC_ARGS[@]}" ./ "${REMOTE_HOST}:${REMOTE_DIR}/"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "==> Dry-run complete"
  exit 0
fi

if [[ "$RESTART_SERVICE" -eq 1 ]]; then
  ssh "$REMOTE_HOST" "systemctl --user restart '${REMOTE_SERVICE}' && systemctl --user --no-pager --full status '${REMOTE_SERVICE}' | sed -n '1,12p'"
fi

REMOTE_VERSION="$(ssh "$REMOTE_HOST" "tr -d '[:space:]' < '${REMOTE_DIR}/VERSION'")"
if [[ "$REMOTE_VERSION" != "$VERSION" ]]; then
  echo "Remote VERSION mismatch: expected ${VERSION}, got ${REMOTE_VERSION}" >&2
  exit 1
fi

ssh "$REMOTE_HOST" "
  set -e
  echo '==> Remote verification'
  echo 'VERSION:' \"\$(cat '${REMOTE_DIR}/VERSION')\"
  echo '-- index asset tags --'
  grep -n 'style.css?v=\|app.js?v=' '${REMOTE_DIR}/static/index.html'
"

echo "==> Deploy OK"
if [[ "$RESTART_SERVICE" -eq 0 ]]; then
  echo "Note: backend/code changes may still need a restart: ./deploy.sh --restart"
fi
