#!/usr/bin/env bash
# FXRoute desktop launcher (installed appliance and live session).
# Waits for the backend (max 180 s wall-clock), then opens Firefox kiosk.
# Never leaves a bare desktop without explanation.
set -Eeuo pipefail

status_url="http://127.0.0.1:8000/api/status"
backend_ready=0
deadline=$(( SECONDS + 180 ))
while (( SECONDS < deadline )); do
  remaining=$(( deadline - SECONDS ))
  request_timeout=$(( remaining < 30 ? remaining : 30 ))
  if curl --fail --silent --show-error --connect-timeout 5 \
      --max-time "$request_timeout" "$status_url" >/dev/null; then
    backend_ready=1
    break
  fi
  sleep 1
done
if [[ "$backend_ready" != 1 ]]; then
  logger -t fxroute-desktop-launcher \
    "FXRoute backend not reachable after 180s; showing the error state" || true
  mkdir -p "$HOME/Desktop" 2>/dev/null || true
  printf '%s\n' \
    'FXRoute could not start: the backend did not come up on 127.0.0.1:8000.' \
    'Diagnosis: journalctl --user -u fxroute -n 50' \
    > "$HOME/Desktop/FXRoute-NOT-STARTED.txt" 2>/dev/null || true
fi
exec firefox --kiosk http://127.0.0.1:8000/
