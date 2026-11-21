#!/usr/bin/env bash
# Confirmed pilot failure recovery, keeping unrelated HA entries intact.
# Usage: rollback_d2.sh --confirmed-failure /config/backups/<backup> <install-lock-token>
# Controller requires Bash, GNU coreutils timeout, ssh and standard tools.
# The commands sent to the Home Assistant target use POSIX/BusyBox sh.
set -euo pipefail
for tool in timeout ssh; do command -v "$tool" >/dev/null || { echo "FEHLER: $tool fehlt" >&2; exit 1; }; done
timeout_version=$(timeout --version 2>/dev/null) || { echo "FEHLER: GNU timeout erforderlich" >&2; exit 1; }
[[ "$timeout_version" == *"GNU coreutils"* ]] || { echo "FEHLER: GNU timeout erforderlich" >&2; exit 1; }
[[ "${1:-}" == --confirmed-failure ]] || { echo 'FEHLER: bestaetigter Installationsfehler erforderlich' >&2; exit 1; }
BACKUP="${2:?backup required}"
TOKEN="${3:?install lock token required}"
[[ "$BACKUP" =~ ^/config/backups/[a-zA-Z0-9._-]+$ ]] || { echo 'FEHLER: ungueltiger Backup-Pfad' >&2; exit 1; }
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
LOCK=/config/backups/nabu-old-integrations/install.lock
RDIR=/config/backups/nabu-old-integrations/recovery-$TOKEN
STAGE=$RDIR/stage
FAILED=$RDIR/failed
CURRENT=/config/custom_components/kinetic_voice_router
DATA=/config/nabu_core
ENTRIES=/config/.storage/core.config_entries
lock_check() { test -f "$LOCK/owner" && test "$(cat "$LOCK/owner")" = "$TOKEN"; }
check_domains() {
  count=0
  for f in /config/custom_components/*/manifest.json; do
    [ -f "$f" ] || continue
    domain=$(jq -er 'select(type == "object") | .domain | select(type == "string" and length > 0)' "$f")
    if [ "$domain" = kinetic_voice_router ]; then
      count=$((count + 1))
      [ "$f" = /config/custom_components/kinetic_voice_router/manifest.json ]
    fi
  done
  [ "$count" -eq 1 ]
}
lock_check
test -f "$LOCK/settled"
test "$(cat "$LOCK/settled")" = "$TOKEN"
cd "$BACKUP"
bad=$(find . ! -type d ! -type f -print -quit)
test -z "$bad"
test -s SHA256SUMS
test -s storage/core.config_entries
test -s nabu_core_data/alarms.sqlite
test -s kinetic_voice_router/manifest.json
sha256sum -c SHA256SUMS >/dev/null
actual=$(find . -type f ! -name SHA256SUMS | LC_ALL=C sort)
listed=$(cut -c 67- SHA256SUMS | LC_ALL=C sort)
test "$actual" = "$listed"
jq -e '[.data.entries[] | select(.domain == "kinetic_voice_router")] | length == 1' storage/core.config_entries >/dev/null
check_domains
test ! -e "$RDIR"
mkdir -m 700 "$RDIR"
mkdir -m 700 "$STAGE" "$FAILED"
cp -a kinetic_voice_router "$STAGE/integration"
cp -a nabu_core_data "$STAGE/nabu_core"
diff -r kinetic_voice_router "$STAGE/integration" >/dev/null
diff -r nabu_core_data "$STAGE/nabu_core" >/dev/null
# A failure or uncertain outcome before confirmed stop leaves all live files untouched.
lock_check
stats=$(ha core stats --no-progress --raw-json </dev/null)
if printf '%s' "$stats" | jq -e '.result == "ok" and (.data.memory_usage | type == "number")' >/dev/null; then
  stop=$(ha core stop --no-progress --raw-json </dev/null)
  printf '%s' "$stop" | jq -e '.result == "ok"' >/dev/null
fi
stopped=0
for attempt in 1 2 3 4 5 6 7 8; do
  stats=$(ha core stats --no-progress --raw-json </dev/null)
  if printf '%s' "$stats" | jq -e '.result == "error" and .error_key == "homeassistant_not_running_error"' >/dev/null; then
    stopped=1; break
  fi
  sleep 2
done
[ "$stopped" -eq 1 ]
supervisor=$(ha supervisor info --no-progress --raw-json </dev/null)
printf '%s' "$supervisor" | jq -e '.result == "ok" and .data.healthy == true' >/dev/null
lock_check
check_domains
test -d "$CURRENT"
test -d "$DATA"
test -s "$ENTRIES"
# Replace only the KVR entry; retain every other entry and all other root fields.
cp -p "$ENTRIES" "$FAILED/core.config_entries"
cp -p "$ENTRIES" "$STAGE/core.config_entries"
jq --slurpfile saved "$BACKUP/storage/core.config_entries" '
  ($saved[0].data.entries | map(select(.domain == "kinetic_voice_router"))) as $old
  | if ($old | length) != 1 or ([.data.entries[] | select(.domain == "kinetic_voice_router")] | length) != 1
    then error("KVR entry count")
    else .data.entries |= map(if .domain == "kinetic_voice_router" then $old[0] else . end) end
' "$ENTRIES" > "$STAGE/entries.generated"
cat "$STAGE/entries.generated" > "$STAGE/core.config_entries"
jq -S '[.data.entries[] | select(.domain == "kinetic_voice_router")]' "$BACKUP/storage/core.config_entries" > "$STAGE/kvr.expected"
jq -S '[.data.entries[] | select(.domain == "kinetic_voice_router")]' "$STAGE/core.config_entries" > "$STAGE/kvr.actual"
cmp "$STAGE/kvr.expected" "$STAGE/kvr.actual"
jq -S '.data.entries |= map(select(.domain != "kinetic_voice_router"))' "$ENTRIES" > "$STAGE/other.before"
jq -S '.data.entries |= map(select(.domain != "kinetic_voice_router"))' "$STAGE/core.config_entries" > "$STAGE/other.after"
cmp "$STAGE/other.before" "$STAGE/other.after"
# Keep the failed candidate, database and config entry for forensic recovery.
# No automatic start is attempted if any move or integrity check fails.
lock_check
mv "$CURRENT" "$FAILED/integration"
mv "$STAGE/integration" "$CURRENT"
mv "$DATA" "$FAILED/nabu_core"
mv "$STAGE/nabu_core" "$DATA"
mv "$STAGE/core.config_entries" "$ENTRIES"
diff -r "$BACKUP/kinetic_voice_router" "$CURRENT" >/dev/null
diff -r "$BACKUP/nabu_core_data" "$DATA" >/dev/null
check_domains
jq -S '[.data.entries[] | select(.domain == "kinetic_voice_router")]' "$ENTRIES" > "$STAGE/kvr.live"
cmp "$STAGE/kvr.expected" "$STAGE/kvr.live"
jq -S '.data.entries |= map(select(.domain != "kinetic_voice_router"))' "$ENTRIES" > "$STAGE/other.live"
cmp "$STAGE/other.before" "$STAGE/other.live"
lock_check
start=$(ha core start --no-progress --raw-json </dev/null)
printf '%s' "$start" | jq -e '.result == "ok"' >/dev/null
echo 'ROLLBACK_STARTED_UNVERIFIED: Lock bleibt; unabhängige Integritaets- und Dienstpruefung erforderlich.'
exit 10
REMOTE
