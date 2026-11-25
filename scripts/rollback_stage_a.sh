#!/usr/bin/env bash
# Nabu Core - Rollback der Stufe A (R32)
#
# Stellt das Integrationsverzeichnis aus dem Vollbackup wieder her, das
# install_stage_a.sh vor dem Tausch angelegt hat. Die Konfigurationseintraege
# werden NICHT angefasst: Stufe A aendert keine Optionen, also gibt es dort
# nichts zurueckzudrehen - jede Aenderung waere ein unnoetiges Risiko.
#
# Aufruf: rollback_stage_a.sh --confirmed-failure /config/backups/nabu-stage-a/<RUN_ID> <lock-token>
#
# Bei unklarem SSH-Ergebnis nicht erneut ausfuehren: der Lock bleibt stehen und
# die Lage muss von Hand geprueft werden.
set -euo pipefail
for tool in timeout ssh; do command -v "$tool" >/dev/null || { echo "FEHLER: $tool fehlt" >&2; exit 1; }; done
timeout_version=$(timeout --version 2>/dev/null) || { echo "FEHLER: GNU timeout erforderlich" >&2; exit 1; }
[[ "$timeout_version" == *"GNU coreutils"* ]] || { echo "FEHLER: GNU timeout erforderlich" >&2; exit 1; }
[[ "${1:-}" == --confirmed-failure ]] || { echo 'FEHLER: bestaetigter Fehler erforderlich (--confirmed-failure)' >&2; exit 1; }
BACKUP="${2:?backup directory required}"
TOKEN="${3:?lock token required}"
[[ "$BACKUP" =~ ^/config/backups/nabu-stage-a/[0-9]{8}-[0-9]{6}-[0-9]+$ ]] || { echo 'FEHLER: ungueltiger Backup-Pfad' >&2; exit 1; }
[[ "$TOKEN" =~ ^[0-9]{8}-[0-9]{6}-[0-9]+$ ]] || { echo 'FEHLER: ungueltiger Lock-Token' >&2; exit 1; }
HA_TARGET="${HA_TARGET:?explicit HA_TARGET required}"
HA_SSH_KEY="${HA_SSH_KEY:?explicit HA_SSH_KEY required}"
[[ "$HA_TARGET" =~ ^root@[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || { echo 'FEHLER: ungueltiges HA-Ziel' >&2; exit 1; }
SSH_TIMEOUT="${HA_SSH_TIMEOUT:-90}"
[[ "$SSH_TIMEOUT" =~ ^[1-9][0-9]{0,2}$ ]] && (( SSH_TIMEOUT <= 180 )) || { echo 'FEHLER: ungueltiger SSH-Timeout' >&2; exit 1; }
SSH=(timeout --signal=TERM --kill-after=5 "${SSH_TIMEOUT}s" ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -i "$HA_SSH_KEY" "$HA_TARGET")

echo 'ROLLBACK: bei SSH-Unklarheit nicht erneut ausfuehren; Lock bleibt bestehen.' >&2
"${SSH[@]}" "BACKUP='$BACKUP' TOKEN='$TOKEN' /bin/sh -s" <<'REMOTE'
set -eu
LOCK=/config/backups/nabu-stage-a.lock
CURRENT=/config/custom_components/kinetic_voice_router
RDIR=$BACKUP/rollback
FAILED=$BACKUP/failed-integration
lock_check() { test -f "$LOCK/owner" && test "$(cat "$LOCK/owner")" = "$TOKEN"; }
check_domains() {
  count=0
  for f in /config/custom_components/*/manifest.json; do
    [ -f "$f" ] || continue
    domain=$(jq -er 'select(type=="object") | .domain | select(type=="string")' "$f")
    if [ "$domain" = kinetic_voice_router ]; then
      count=$((count + 1))
      [ "$f" = "$CURRENT/manifest.json" ]
    fi
  done
  [ "$count" -eq 1 ]
}
lock_check
test -d "$BACKUP/integration"
test -s "$BACKUP/SHA256SUMS"
cd "$BACKUP"
bad=$(find . ! -type d ! -type f -print -quit)
test -z "$bad"
sha256sum -c --quiet SHA256SUMS
test -d "$BACKUP/integration/nabu_core"
check_domains
test ! -e "$RDIR"

stop_guard() {
  set -eu
  stats=$(ha core stats --no-progress --raw-json </dev/null)
  printf '%s' "$stats" | jq -e '.result == "error" and .error_key == "homeassistant_not_running_error"' >/dev/null
  supervisor=$(ha supervisor info --no-progress --raw-json </dev/null)
  printf '%s' "$supervisor" | jq -e '.result == "ok" and .data.healthy == true' >/dev/null
}

mkdir -m 700 "$RDIR"
lock_check
stats=$(ha core stats --no-progress --raw-json </dev/null)
if printf '%s' "$stats" | jq -e '.result == "ok" and (.data.memory_usage | type == "number")' >/dev/null; then
  stop=$(ha core stop --no-progress --raw-json </dev/null)
  printf '%s' "$stop" | jq -e '.result == "ok"' >/dev/null
fi
stopped=0
for _ in 1 2 3 4 5 6 7 8; do
  if stop_guard; then stopped=1; break; fi
  sleep 2
done
[ "$stopped" -eq 1 ]

# Erst nach bestaetigtem Stop wird getauscht: gestoppt -> alt weg, Backup rein.
lock_check
check_domains
test -d "$CURRENT"
mv "$CURRENT" "$FAILED"
mv "$BACKUP/integration" "$CURRENT"
diff -r "$CURRENT" "$FAILED" >/dev/null 2>&1 && echo "WARNUNG: Kandidat und Backup identisch" >&2 || true
check_domains
count=$(find "$CURRENT" -type f -not -path '*__pycache__*' | wc -l)
test "$count" -eq 42

lock_check
start=$(ha core start --no-progress --raw-json </dev/null)
printf '%s' "$start" | jq -e '.result == "ok"' >/dev/null
sleep 45
stats=$(ha core stats --no-progress --raw-json </dev/null)
printf '%s' "$stats" | jq -e '.result == "ok" and (.data.memory_usage | type == "number")' >/dev/null
echo "ROLLBACK_STARTED_UNVERIFIED: Lock bleibt. Unabhaengige Dienst- und Dateipruefung erforderlich."
exit 10
REMOTE
