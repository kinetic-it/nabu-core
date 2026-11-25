#!/usr/bin/env bash
# Nabu Core - Stufen-Installation (R32): tauscht genau die Dateien, die im
# Paket-SHA256SUMS stehen (ausschliesslich nabu_core/*.py).
#
# Gelernt aus R19 (install_d2.sh) und uebernommen:
#   * Ein Vollbackup liegt AUSSERHALB von custom_components. Ein Unterordner mit
#     eigener manifest.json wuerde als zweite Integration geladen und erzeugt
#     fremde Scheduler-Instanzen.
#   * Ein exklusiver Lock mit Token schuetzt jeden Schritt; bei unklarem
#     SSH-Ergebnis bleibt der Lock stehen und es wird nichts weiter getan.
#   * Erst ein positiver Supervisor-Befund gilt als "gestoppt" bzw. "laeuft".
#   * Dateitausch nur nach bestaetigtem Stop, jede Datei per .tmp + mv.
#   * Erfolg wird nicht behauptet: nach dem Start endet der Lauf mit
#     "INSTALLED_UNVERIFIED" (Exit 10). Der Lock wird erst mit --settle
#     freigegeben, nachdem eine unabhaengige Pruefung den Dienst bestaetigt hat.
#
# Aufruf:
#   install_stage_a.sh <stage-verzeichnis>
#   install_stage_a.sh --settle <lock-token>
#
# Umgebung: HA_TARGET=root@host  HA_SSH_KEY=/pfad/zum/key
#
# Der Stage enthaelt:
#   SHA256SUMS               (Pflicht, bestimmt die Dateiliste)
#   nabu_core/<modul>.py     je Eintrag genau eine Datei
#
# Nicht enthalten und ausdruecklich unberuehrt: manifest.json, const.py,
# Optionen (alarm_media, cycle_on/off), /media-Dateien, Datenbank.
set -euo pipefail
for tool in timeout ssh; do command -v "$tool" >/dev/null || { echo "FEHLER: $tool fehlt" >&2; exit 1; }; done
timeout_version=$(timeout --version 2>/dev/null) || { echo "FEHLER: GNU timeout erforderlich" >&2; exit 1; }
[[ "$timeout_version" == *"GNU coreutils"* ]] || { echo "FEHLER: GNU timeout erforderlich" >&2; exit 1; }

HA_TARGET="${HA_TARGET:?explicit HA_TARGET required (root@host)}"
HA_SSH_KEY="${HA_SSH_KEY:?explicit HA_SSH_KEY required}"
[[ "$HA_TARGET" =~ ^root@[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || { echo "FEHLER: ungueltiges HA-Ziel" >&2; exit 1; }
SSH_TIMEOUT="${HA_SSH_TIMEOUT:-90}"
[[ "$SSH_TIMEOUT" =~ ^[1-9][0-9]{0,2}$ ]] && (( SSH_TIMEOUT <= 180 )) || { echo "FEHLER: ungueltiger SSH-Timeout" >&2; exit 1; }
SSH=(timeout --signal=TERM --kill-after=5 "${SSH_TIMEOUT}s" ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -i "$HA_SSH_KEY" "$HA_TARGET")

# Welche Dateien getauscht werden, steht im Paket selbst (SHA256SUMS), nicht im
# Skript: sonst muesste das Werkzeug fuer jede Kandidatengroesse angefasst werden.
FILES=()
CURRENT=/config/custom_components/kinetic_voice_router
BACKUP_ROOT=/config/backups/nabu-stage-a
LOCK_DIR=/config/backups/nabu-stage-a.lock

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

if [ "${1:-}" = "--settle" ]; then
  TOKEN="${2:?lock token required}"
  [[ "$TOKEN" =~ ^[0-9]{8}-[0-9]{6}-[0-9]+$ ]] || { echo "FEHLER: ungueltiger Lock-Token" >&2; exit 1; }
  "${SSH[@]}" "set -eu
    test -f '$LOCK_DIR/owner'
    test \"\$(cat '$LOCK_DIR/owner')\" = '$TOKEN'
    test -f '$LOCK_DIR/installed'
    $RUNNING_GUARD
    # Der Nachweis gehoert in eine Datei, nicht in das Verzeichnis: ein Marker im
    # Lock-Verzeichnis verhindert das abschliessende rmdir.
    printf '%s settled %s\n' '$TOKEN' \"\$(date -Is)\" >> '$BACKUP_ROOT/settled.log'
    rm -f '$LOCK_DIR/owner' '$LOCK_DIR/installed'
    rmdir '$LOCK_DIR'"
  echo "SETTLED: Lock freigegeben."
  exit 0
fi

STAGE="${1:?stage directory required}"
test -d "$STAGE" || { echo "FEHLER: Stage fehlt" >&2; exit 1; }
test -f "$STAGE/SHA256SUMS" || { echo "FEHLER: SHA256SUMS fehlt" >&2; exit 1; }
mapfile -t stage_entries < <(awk 'NF >= 2 {print $NF}' "$STAGE/SHA256SUMS")
(( ${#stage_entries[@]} >= 1 )) || { echo "FEHLER: SHA256SUMS ist leer" >&2; exit 1; }
for entry in "${stage_entries[@]}"; do
  [[ "$entry" =~ ^nabu_core/[a-z0-9_]+\.py$ ]] || {
    echo "FEHLER: unerlaubter Manifestpfad: $entry" >&2
    exit 1
  }
  FILES+=("${entry#nabu_core/}")
done
(( ${#FILES[@]} == $(printf '%s\n' "${FILES[@]}" | sort -u | wc -l) )) || {
  echo "FEHLER: doppelter Eintrag in SHA256SUMS" >&2
  exit 1
}
for f in "${FILES[@]}"; do
  test -s "$STAGE/nabu_core/$f" || { echo "FEHLER: Stage unvollstaendig: $f" >&2; exit 1; }
done
( cd "$STAGE" && sha256sum -c SHA256SUMS >/dev/null ) || { echo "FEHLER: Stage-Hashes stimmen nicht" >&2; exit 1; }
(( $(find "$STAGE" -type f | wc -l) == ${#FILES[@]} + 1 )) || {
  echo "FEHLER: Stage enthaelt fremde Dateien" >&2
  exit 1
}

FILE_LIST="${FILES[*]}"
RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
echo "RUN_ID=$RUN_ID"

echo "== Preflight: Live-Bestand =="
live_preflight() {
"${SSH[@]}" "set -eu
  test -d '$CURRENT/nabu_core'
  count=\$(find '$CURRENT' -type f -not -path '*__pycache__*' | wc -l)
  test \"\$count\" -eq 42
  test -s '$CURRENT/manifest.json'
  test -s '$CURRENT/const.py'
  # Nur vorhandene Dateien tauschen: ein Paket darf die Integration nicht erweitern.
  for f in $FILE_LIST; do
    test -s '$CURRENT/nabu_core/'\$f
  done
  # Keine fremde Kopie derselben Domain (R19-Befund).
  found=0
  for m in /config/custom_components/*/manifest.json; do
    [ -f \"\$m\" ] || continue
    d=\$(jq -er 'select(type==\"object\") | .domain | select(type==\"string\")' \"\$m\")
    [ \"\$d\" = kinetic_voice_router ] || continue
    found=\$((found + 1))
    [ \"\$m\" = '$CURRENT/manifest.json' ]
  done
  test \"\$found\" -eq 1"
}
live_preflight || { echo "FEHLER: Live-Preflight fehlgeschlagen (Dateizahl 42, manifest.json/const.py oder Domain-Eindeutigkeit)" >&2; exit 1; }
if [ "${2:-}" = "--preflight-only" ]; then
  echo "PREFLIGHT_ONLY_OK: keine Aenderung vorgenommen."
  exit 0
fi

echo "== Vollbackup ausserhalb custom_components =="
"${SSH[@]}" "set -eu
  test ! -e '$BACKUP_ROOT/$RUN_ID'
  mkdir -p '$BACKUP_ROOT/$RUN_ID'
  cp -a '$CURRENT' '$BACKUP_ROOT/$RUN_ID/integration'
  cp -a /config/nabu_core '$BACKUP_ROOT/$RUN_ID/nabu_core_data'
  cp -a /config/.storage/core.config_entries '$BACKUP_ROOT/$RUN_ID/core.config_entries'
  cd '$BACKUP_ROOT/$RUN_ID'
  find integration nabu_core_data -type f -not -path '*__pycache__*' ! -name SHA256SUMS | LC_ALL=C sort | xargs sha256sum > SHA256SUMS
  sha256sum core.config_entries >> SHA256SUMS
  diff -r '$CURRENT' 'integration' >/dev/null
  diff -r /config/nabu_core 'nabu_core_data' >/dev/null
  bad=\$(find . ! -type d ! -type f -print -quit)
  test -z \"\$bad\"
  test -s SHA256SUMS"
echo "BACKUP=$BACKUP_ROOT/$RUN_ID"

echo "== Lock =="
LOCK_GUARD="set -eu; test -f '$LOCK_DIR/owner'; test \"\$(cat '$LOCK_DIR/owner')\" = '$RUN_ID'"
"${SSH[@]}" "set -eu; mkdir '$LOCK_DIR'; printf '%s' '$RUN_ID' > '$LOCK_DIR/owner'" || { rc=$?; echo "FEHLER: Lock nicht erworben" >&2; exit "$rc"; }

stop_attempted=0; stop_accepted=0; start_attempted=0; swapped=0
cleanup() {
  rc=$?
  trap - EXIT
  case "$rc" in 124|137|255)
    echo "FEHLER: SSH-Ergebnis unklar; Lock bleibt, keine konkurrierende Recovery" >&2
    return "$rc" ;; esac
  if [ "$swapped" -eq 0 ]; then
    # Nichts getauscht, aber vielleicht gestoppt: Dienst wieder in Betrieb nehmen.
    if [ "$stop_accepted" -eq 1 ] && [ "$start_attempted" -eq 0 ]; then
      if "${SSH[@]}" "$LOCK_GUARD; $RUNNING_GUARD || ha core start"; then
        echo "Hinweis: Core nach Abbruch wieder gestartet (unbestaetigt, bitte pruefen)." >&2
      else
        echo "FEHLER: Core nicht bestaetigt gestartet; Lock bleibt." >&2
        return "$rc"
      fi
    fi
    "${SSH[@]}" "$LOCK_GUARD; rm '$LOCK_DIR/owner'; rmdir '$LOCK_DIR'" >/dev/null 2>&1 || echo "FEHLER: Lock bleibt bestehen" >&2
  else
    echo "FEHLER: Dateien getauscht; Rollback nur ueber rollback_stage_a.sh (Token $RUN_ID)" >&2
  fi
  return "$rc"
}
trap cleanup EXIT

echo "== Stage uebertragen =="
tar -C "$STAGE" -c . | "${SSH[@]}" "$LOCK_GUARD; set -eu
  mkdir '$BACKUP_ROOT/$RUN_ID/stage'
  tar -x -C '$BACKUP_ROOT/$RUN_ID/stage'
  cd '$BACKUP_ROOT/$RUN_ID/stage'
  sha256sum -c SHA256SUMS >/dev/null"

echo "== HA stoppen =="
stop_attempted=1
"${SSH[@]}" "$LOCK_GUARD; ha core stop"
stop_accepted=1
for _ in 1 2 3 4 5 6 7 8; do
  if "${SSH[@]}" "$LOCK_GUARD; $STOP_GUARD"; then break; fi
  sleep 2
done
"${SSH[@]}" "$LOCK_GUARD; $STOP_GUARD" || { echo "FEHLER: Core-Stopp nicht bestaetigt; kein Dateitausch" >&2; exit 1; }

echo "== Dateien tauschen (${#FILES[@]} Dateien) =="
# Erst alle als .r32.new ablegen und einzeln pruefen, dann in einem Zug
# umbenennen: ein Fehler beim Kopieren hinterlaesst so keinen halben Tausch (R19).
"${SSH[@]}" "$LOCK_GUARD; set -eu
  $STOP_GUARD
  stage='$BACKUP_ROOT/$RUN_ID/stage/nabu_core'
  live='$CURRENT/nabu_core'
  for f in $FILE_LIST; do
    test -s \"\$stage/\$f\"
    cp -p \"\$stage/\$f\" \"\$live/\$f.r32.new\"
    test \"\$(sha256sum \"\$live/\$f.r32.new\" | cut -d' ' -f1)\" = \"\$(sha256sum \"\$stage/\$f\" | cut -d' ' -f1)\"
  done
  for f in $FILE_LIST; do
    mv \"\$live/\$f.r32.new\" \"\$live/\$f\"
  done"
swapped=1

"${SSH[@]}" "$LOCK_GUARD; set -eu
  stage='$BACKUP_ROOT/$RUN_ID/stage/nabu_core'
  live='$CURRENT/nabu_core'
  for f in $FILE_LIST; do
    expected=\$(sha256sum \"\$stage/\$f\" | cut -d' ' -f1)
    actual=\$(sha256sum \"\$live/\$f\" | cut -d' ' -f1)
    test \"\$expected\" = \"\$actual\"
  done
  count=\$(find '$CURRENT' -type f -not -path '*__pycache__*' | wc -l)
  test \"\$count\" -eq 42
  test \"\$(find '$CURRENT' -name '*.tmp' -o -name '*.r32.new' | wc -l)\" -eq 0
  # Unberuehrt: Manifest, const.py und die Optionen.
  diff -q '$CURRENT/manifest.json' '$BACKUP_ROOT/$RUN_ID/integration/manifest.json'
  diff -q '$CURRENT/const.py' '$BACKUP_ROOT/$RUN_ID/integration/const.py'
  diff -q /config/.storage/core.config_entries '$BACKUP_ROOT/$RUN_ID/core.config_entries'"

echo "== HA starten =="
start_attempted=1
"${SSH[@]}" "$LOCK_GUARD; ha core start"
sleep 45
"${SSH[@]}" "$LOCK_GUARD; $RUNNING_GUARD" || { echo "FEHLER: Core nicht bestaetigt gestartet" >&2; exit 1; }
logs=$("${SSH[@]}" "$LOCK_GUARD; ha core logs") || { echo "FEHLER: Logs nicht lesbar" >&2; exit 1; }
if printf '%s\n' "$logs" | grep -iE '(ERROR|CRITICAL).*kinetic_voice_router|kinetic_voice_router.*(error|traceback)|error setting up entry Kinetic Voice Router' >/dev/null; then
  "${SSH[@]}" "$LOCK_GUARD; printf '%s' '$RUN_ID' > '$LOCK_DIR/installed'"
  echo "FEHLER: Router-Initialisierung im Log auffaellig; Rollback vorbereiten" >&2
  exit 1
fi
"${SSH[@]}" "$LOCK_GUARD; printf '%s' '$RUN_ID' > '$LOCK_DIR/installed'"
trap - EXIT
echo "INSTALLED_UNVERIFIED: Exit 10. Lock bleibt bis 'install_stage_a.sh --settle $RUN_ID'."
exit 10
