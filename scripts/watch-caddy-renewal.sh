#!/usr/bin/env bash
# Read-only watcher for the optional Caddy proxy of an FXRoute install.
# Logs service health every 15 min and records full evidence for every
# automatic TLS renewal of the confined (httpd_t) Caddy process.
#
# usage: watch-caddy-renewal.sh <deadline-epoch>
#
# All watcher output goes to the log file; stdout is left untouched, so a
# nohup start does not lose evidence. Environment overrides (defaults suit
# a normal install) exist for smoke tests:
#   FXROUTE_WATCH_BASE           cert storage base directory
#   FXROUTE_WATCH_LOG            log file path
#   FXROUTE_WATCH_UNIT           systemd unit name
#   FXROUTE_WATCH_URL            health probe URL
#   FXROUTE_WATCH_POLL           poll interval in seconds
#   FXROUTE_WATCH_JOURNAL_SINCE  journal window used for renewal evidence
#   FXROUTE_WATCH_SUDO           1 (default) = use sudo -n for cert access
#   FXROUTE_WATCH_HEALTH_EVERY   health log interval in seconds
set -u

DEADLINE_EPOCH="${1:?usage: watch-caddy-renewal.sh <deadline-epoch>}"
BASE="${FXROUTE_WATCH_BASE:-/var/lib/fxroute-caddy/caddy/certificates/local}"
LOG="${FXROUTE_WATCH_LOG:-/tmp/fxroute-renewal-watch.log}"
UNIT="${FXROUTE_WATCH_UNIT:-fxroute-caddy}"
# Caddy serves the named sites only, so probe one of them; a bare loopback
# address gets no SNI match and no certificate.
URL="${FXROUTE_WATCH_URL:-https://fxroute.local/api/status}"
POLL="${FXROUTE_WATCH_POLL:-60}"
JOURNAL_SINCE="${FXROUTE_WATCH_JOURNAL_SINCE:--15 min}"
USE_SUDO="${FXROUTE_WATCH_SUDO:-1}"
HEALTH_EVERY="${FXROUTE_WATCH_HEALTH_EVERY:-900}"
LOCAL_DIR="$BASE/fxroute.local"
IP_DIR="$BASE/192.168.178.104"

log() { printf '%s %s\n' "$(date '+%F %T') $*" >>"$LOG"; }

# Read the root-owned cert store, optionally through sudo -n.
priv() { if [ "$USE_SUDO" = "1" ]; then sudo -n "$@"; else "$@"; fi; }

# Fail fast if the storage paths or cert access are broken.
for d in "$LOCAL_DIR" "$IP_DIR"; do
  [ "$(priv find "$d" -name '*.crt' 2>/dev/null | wc -l)" -gt 0 ] || { log "FATAL: cannot poll $d"; exit 4; }
done

newest_epoch() { priv find "$1" -name '*.crt' -printf '%T@\n' 2>/dev/null | sort -n | tail -1; }

health_line() {
  local st ctx code
  st=$(systemctl show "$UNIT" -p ActiveState -p SubState -p NRestarts | tr '\n' ' ')
  ctx=$(ps -C caddy -o label= 2>/dev/null | head -1)
  code=$(curl -sk -o /dev/null -w '%{http_code}' "$URL")
  log "HEALTH $st|ctx=$ctx|https=$code"
}

# Renewal evidence: service state, process context, cert data, journal.
renewal_evidence() {
  local st ctx dates jout line
  st=$(systemctl show "$UNIT" -p ActiveState -p SubState -p NRestarts | tr '\n' ' ')
  ctx=$(ps -C caddy -o label= 2>/dev/null | head -1)
  dates=$(priv openssl x509 -in "$LOCAL_DIR/fxroute.local.crt" -noout -dates 2>&1 | tr '\n' ';')
  log "RENEWAL DETECTED"
  log "SERVICE $st"
  log "PROC_CONTEXT $ctx"
  log "CERT_DATES $dates"
  jout=$(journalctl -u "$UNIT" --since "$JOURNAL_SINCE" --no-pager 2>/dev/null \
    | grep -Ei 'renew|denied|permission|error' | tail -40)
  if [ -n "$jout" ]; then
    while IFS= read -r line; do log "JOURNAL $line"; done <<<"$jout"
  else
    log "JOURNAL (no matching lines in the last ${JOURNAL_SINCE#- })"
  fi
}

renewals=0
lastlocal=$(newest_epoch "$LOCAL_DIR"); lastip=$(newest_epoch "$IP_DIR")
log "watch start (cert base $BASE) baseline local=$lastlocal ip=$lastip"
health_line
last_health=$(date +%s)

while :; do
  now=$(date +%s)
  if [ "$now" -ge "$DEADLINE_EPOCH" ]; then
    log "deadline reached; final state"
    health_line
    log "RESULT: renewals=$renewals (last local=$lastlocal ip=$lastip)"
    exit 0
  fi
  sleep "$POLL"
  nl=$(newest_epoch "$LOCAL_DIR"); ni=$(newest_epoch "$IP_DIR")
  if [ "$nl" != "$lastlocal" ] || [ "$ni" != "$lastip" ]; then
    lastlocal=$nl; lastip=$ni
    renewals=$((renewals + 1))
    renewal_evidence
    health_line
    last_health=$(date +%s)
    continue
  fi
  if [ $(( $(date +%s) - last_health )) -ge "$HEALTH_EVERY" ]; then
    health_line
    last_health=$(date +%s)
  fi
done
