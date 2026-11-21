#!/usr/bin/env bash
# Nabu Core — kontrollierte Pilotinstallation (R09.5, korrigierte Fassung)
#
# Aenderung gegenueber install_d1.sh:
#   Das bisherige Integrationsverzeichnis wird NICHT innerhalb von
#   /config/custom_components/ gesichert, sondern nach
#   /config/backups/nabu-old-integrations/ verschoben.
#   Grund: Home Assistant laedt jedes Unterverzeichnis von custom_components/
#   als eigene Integration. Mehrere Kopien mit derselben domain fuehren zu
#   getrennten Scheduler-Instanzen; alarm_stop wirkte dann nicht (R09.5).
#
# Aufruf: install_d2.sh <stage-verzeichnis> <aktuelles-vollbackup-unter-config-backups>
# Controller requires Bash, GNU coreutils timeout, ssh and standard tools.
# The commands sent to the Home Assistant target use POSIX/BusyBox sh.
set -euo pipefail
for tool in timeout ssh; do command -v "$tool" >/dev/null || { echo "FEHLER: $tool fehlt" >&2; exit 1; }; done
timeout_version=$(timeout --version 2>/dev/null) || { echo "FEHLER: GNU timeout erforderlich" >&2; exit 1; }
[[ "$timeout_version" == *"GNU coreutils"* ]] || { echo "FEHLER: GNU timeout erforderlich" >&2; exit 1; }

STAGE="${1:?stage directory required}"
BACKUP="${2:?backup directory required}"
[[ "$BACKUP" =~ ^/config/backups/[a-zA-Z0-9._-]+$ ]] || { echo "FEHLER: ungueltiger Backup-Pfad"; exit 1; }
HA_TARGET="${HA_TARGET:?explicit HA_TARGET required (root@host)}"
HA_SSH_KEY="${HA_SSH_KEY:?explicit HA_SSH_KEY required}"
[[ "$HA_TARGET" =~ ^root@[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || { echo "FEHLER: ungueltiges HA-Ziel"; exit 1; }
SSH_TIMEOUT="${HA_SSH_TIMEOUT:-90}"
[[ "$SSH_TIMEOUT" =~ ^[1-9][0-9]{0,2}$ ]] && (( SSH_TIMEOUT <= 180 )) || { echo "FEHLER: ungueltiger SSH-Timeout"; exit 1; }
SSH=(timeout --signal=TERM --kill-after=5 "${SSH_TIMEOUT}s" ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -i "$HA_SSH_KEY" "$HA_TARGET")
TS=$(date +%Y%m%d-%H%M%S)
RUN_ID="${TS}-$$"

echo "== Preflight =="
test -d "$STAGE" || { echo "STAGE fehlt"; exit 1; }
test -f "$STAGE/manifest.json" || { echo "manifest.json fehlt im Stage"; exit 1; }
python3 - "$STAGE" <<'PY_STAGE'
import os
import stat
import sys

root = sys.argv[1]
if not stat.S_ISDIR(os.lstat(root).st_mode):
    raise SystemExit("FEHLER: Stage ist kein echtes Verzeichnis")
def fail(exc):
    raise exc
for base, dirs, files in os.walk(root, followlinks=False, onerror=fail):
    for name in dirs + files:
        mode = os.lstat(os.path.join(base, name)).st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise SystemExit("FEHLER: Stage enthaelt Link oder Sonderdatei")
PY_STAGE
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert isinstance(d,dict) and d.get("domain")=="kinetic_voice_router"' "$STAGE/manifest.json" || { echo "ungueltige Stage-Domain"; exit 1; }

# Pruefung anhand aller Manifest-Domains, nicht der Verzeichnisnamen.
check_domains() {
  "${SSH[@]}" 'set -eu
    count=0
    for f in /config/custom_components/*/manifest.json; do
      [ -f "$f" ] || continue
      domain=$(jq -er "select(type == \"object\") | .domain | select(type == \"string\" and length > 0)" "$f") || { echo "Ungueltiges Manifest: $f" >&2; exit 1; }
      if [ "$domain" = kinetic_voice_router ]; then
        count=$((count + 1))
        [ "$f" = /config/custom_components/kinetic_voice_router/manifest.json ] || { echo "Fremde Kopie: $f" >&2; exit 1; }
      fi
    done
    [ "$count" -eq 1 ] || { echo "Domain-Anzahl: $count" >&2; exit 1; }
    echo "Manifest-Domain eindeutig"
  '
}
# A positive Supervisor response, not a process-name guess, gates every move.
STOP_GUARD=$(cat <<'GUARD'
set -eu
stats=$(ha core stats --no-progress --raw-json)
printf '%s' "$stats" | jq -e '.result == "error" and .error_key == "homeassistant_not_running_error"' >/dev/null
supervisor=$(ha supervisor info --no-progress --raw-json)
printf '%s' "$supervisor" | jq -e '.result == "ok" and .data.healthy == true' >/dev/null
GUARD
)
RUNNING_GUARD=$(cat <<'GUARD'
set -eu
stats=$(ha core stats --no-progress --raw-json)
printf '%s' "$stats" | jq -e '.result == "ok" and (.data.memory_usage | type == "number")' >/dev/null
GUARD
)
echo "== Backup-Preflight =="
"${SSH[@]}" "set -eu; cd '$BACKUP'; bad=\$(find . ! -type d ! -type f -print -quit); test -z \"\$bad\"" || { echo "FEHLER: Backup enthaelt Link oder Sonderdatei" >&2; exit 1; }
"${SSH[@]}" "set -e; cd '$BACKUP'; test -s SHA256SUMS; test -s storage/core.config_entries; test -s nabu_core_data/alarms.sqlite; test -s kinetic_voice_router/manifest.json; grep -Eq '^[0-9A-Fa-f]{64} [ *][.]/storage/core[.]config_entries$' SHA256SUMS; grep -Eq '^[0-9A-Fa-f]{64} [ *][.]/nabu_core_data/alarms[.]sqlite$' SHA256SUMS; grep -Eq '^[0-9A-Fa-f]{64} [ *][.]/kinetic_voice_router/manifest[.]json$' SHA256SUMS; sha256sum -c SHA256SUMS >/dev/null" || { echo "FEHLER: Vollbackup unvollstaendig oder beschaedigt"; exit 1; }
# The backup must enumerate every file and match current DB/options.
BACKUP_GUARD="set -eu
cd '$BACKUP'
bad=\$(find . ! -type d ! -type f -print -quit)
test -z \"\$bad\"
actual=\$(find . -type f ! -name SHA256SUMS | LC_ALL=C sort)
listed=\$(cut -c 67- SHA256SUMS | LC_ALL=C sort)
test \"\$actual\" = \"\$listed\"
test ! -e /config/nabu_core/alarms.sqlite-wal
cmp nabu_core_data/alarms.sqlite /config/nabu_core/alarms.sqlite
cmp storage/core.config_entries /config/.storage/core.config_entries"
"${SSH[@]}" "$BACKUP_GUARD" || { echo "FEHLER: Backup nicht aktuell oder unvollstaendig" >&2; exit 1; }
echo "== Domain-Preflight =="
check_domains || { rc=$?; echo "FEHLER: Domain-Preflight" >&2; exit "$rc"; }

# Persistent, atomic per-target lock.  It is intentionally retained after a
# stop/start or any uncertain SSH outcome until independent verification.
LOCK_DIR=/config/backups/nabu-old-integrations/install.lock
LOCK_GUARD="set -eu; test -f '$LOCK_DIR/owner'; test \"\$(cat '$LOCK_DIR/owner')\" = '$RUN_ID'"
"${SSH[@]}" "set -eu; mkdir -p /config/backups/nabu-old-integrations; mkdir '$LOCK_DIR'; printf '%s' '$RUN_ID' > '$LOCK_DIR/owner'" || { rc=$?; echo "FEHLER: Installations-Lock nicht erworben" >&2; exit "$rc"; }
echo "INSTALL_LOCK_TOKEN=$RUN_ID"
stop_attempted=0
stop_accepted=0
start_attempted=0
release_lock() {
  "${SSH[@]}" "$LOCK_GUARD
    rm '$LOCK_DIR/owner'
    rmdir '$LOCK_DIR'"
}
mark_settled() {
  "${SSH[@]}" "$LOCK_GUARD
    printf '%s' '$RUN_ID' > '$LOCK_DIR/settled'"
}
recover_before_start() {
  rc=$?
  trap - EXIT
  case "$rc" in 124|137|255)
    echo "FEHLER: SSH-Ergebnis unklar; Lock bleibt, keine konkurrierende Recovery" >&2
    return "$rc" ;;
  esac
  if [ "$stop_attempted" -eq 0 ]; then
    release_lock || echo "FEHLER: Lock bleibt bestehen" >&2
    return "$rc"
  fi
  if [ "$stop_accepted" -eq 0 ]; then
    echo "FEHLER: Stopstatus unklar; Lock bleibt, keine automatische Dateiaenderung" >&2
    return "$rc"
  fi
  if [ "$start_attempted" -ne 0 ]; then
    echo "FEHLER: Startstatus unklar; Lock bleibt, keine automatische Dateiaenderung" >&2
    return "$rc"
  fi
  echo "FEHLER: versuche Rollback vor Start" >&2
  if "${SSH[@]}" "$LOCK_GUARD
    $STOP_GUARD
    old=/config/backups/nabu-old-integrations/kinetic_voice_router-$RUN_ID
    current=/config/custom_components/kinetic_voice_router
    failed=/config/backups/nabu-old-integrations/kv-failed-$RUN_ID
    if [ -d \"\$old\" ]; then
      if [ -e \"\$current\" ]; then test ! -e \"\$failed\"; mv \"\$current\" \"\$failed\"; fi
      mv \"\$old\" \"\$current\"
    fi
    test -d \"\$current\"
    diff -r '$BACKUP/kinetic_voice_router' \"\$current\" >/dev/null" && check_domains && "${SSH[@]}" "$LOCK_GUARD
ha core start"; then
    echo "Rollback-Restart angefordert; Lock bleibt bis unabhängiger Prüfung" >&2
  else
    echo "FEHLER: Rollback nicht bestaetigt; Lock bleibt, kein weiterer Start" >&2
  fi
  return "$rc"
}
trap recover_before_start EXIT

echo "== Stage kopieren (vor HA-Stopp) =="
tar -C "$STAGE" -c . | "${SSH[@]}" "$LOCK_GUARD
set -e; mkdir /config/backups/nabu-old-integrations/kv-new-$RUN_ID; tar -x -C /config/backups/nabu-old-integrations/kv-new-$RUN_ID
  bad=\$(find /config/backups/nabu-old-integrations/kv-new-$RUN_ID ! -type d ! -type f -print -quit)
  test -z \"\$bad\""
echo "== HA stoppen =="
stop_attempted=1
"${SSH[@]}" "$LOCK_GUARD
ha core stop" || { rc=$?; echo "FEHLER: HA-Stopp nicht bestaetigt" >&2; exit "$rc"; }
stop_accepted=1
wait_stopped() {
  for _ in 1 2 3 4 5 6 7 8; do
    if "${SSH[@]}" "$LOCK_GUARD
$STOP_GUARD"; then return 0; else rc=$?; fi
    case "$rc" in 124|137|255) return "$rc" ;; esac
    sleep 2
  done
  return 1
}
wait_stopped || { rc=$?; echo "FEHLER: Core-Stopp nicht eindeutig; kein Dateitausch" >&2; exit "$rc"; }

echo "== Alte Integration AUSSERHALB von custom_components sichern =="
"${SSH[@]}" "$LOCK_GUARD
$STOP_GUARD
$BACKUP_GUARD
  test -d /config/custom_components/kinetic_voice_router
  test ! -e /config/backups/nabu-old-integrations/kinetic_voice_router-$RUN_ID
  mv /config/custom_components/kinetic_voice_router /config/backups/nabu-old-integrations/kinetic_voice_router-$RUN_ID
  mv /config/backups/nabu-old-integrations/kv-new-$RUN_ID /config/custom_components/kinetic_voice_router"
echo "== Domain-Verifikation vor HA-Start =="
check_domains || { rc=$?; echo "FEHLER: Vor-Start-Pruefung" >&2; exit "$rc"; }
echo "== HA starten =="
start_attempted=1
"${SSH[@]}" "$LOCK_GUARD
ha core start"
sleep 45
echo "== Verifikation: genau eine Manifest-Domain =="
check_domains || { rc=$?; echo "FEHLER: Domain-Verifikation" >&2; exit "$rc"; }
echo "== Verifikation: Core laeuft =="
"${SSH[@]}" "$RUNNING_GUARD" || { rc=$?; echo "FEHLER: Core nicht bestaetigt" >&2; exit "$rc"; }
echo "== Verifikation: Log =="
logs=$("${SSH[@]}" "ha core logs") || { rc=$?; echo "FEHLER: Logs nicht lesbar" >&2; exit "$rc"; }
if printf '%s\n' "$logs" | grep -iE '(ERROR|CRITICAL).*kinetic_voice_router|kinetic_voice_router.*(error|traceback)|error setting up entry Kinetic Voice Router' >/dev/null; then
  mark_settled
  echo "FEHLER: Router-Initialisierung im Log auffaellig" >&2
  exit 1
fi
mark_settled
trap - EXIT
echo "STARTED_UNVERIFIED: Lock bleibt bis zur unabhaengigen Dienst-, Daten- und Dateiverifikation."
exit 10
