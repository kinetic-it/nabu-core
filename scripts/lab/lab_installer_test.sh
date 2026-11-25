#!/usr/bin/env bash
# Laborpruefung des Stufe-A-Installers gegen eine Wegwerf-Wurzel (kein produktives Ziel).
#
#   A  Erfolgspfad              -> vier Dateien getauscht, Exit 10, Lock bleibt
#   B  Fehler NACH dem Tausch   -> Meldung "Dateien getauscht", kein "Abbruch vor Tausch"
#   C  Kopierfehler             -> kein halber Tausch, Live-Dateien unveraendert
#   D  BusyBox-sha256sum        -> Installer laeuft ohne --quiet durch
set -uo pipefail

LAB_BASE="${LAB_BASE:-$HOME/work/r32-lab-installer}"
INSTALLER="${INSTALLER:-/tmp/install_lab.sh}"
STAGE_SRC="${STAGE_SRC:-$HOME/work/r32-stage-a}"
PASS=0
FAIL=0

CURRENT_REL="config/custom_components/kinetic_voice_router"
BACKUP_REL="config/backups/nabu-stage-a"

prepare_root() {
  local root="$1"
  rm -rf "$root"
  mkdir -p "$root/$CURRENT_REL/nabu_core" "$root/config/nabu_core" "$root/config/.storage" "$root/bin"
  (
    cd "$root/$CURRENT_REL"
    for name in __init__.py alarm_bridge.py assist_gate.py config_flow.py const.py conversation.py \
                diagnostics.py hermes_client.py runtime.py speech_store.py status.py texts.py; do
      printf 'original %s\n' "$name" > "$name"
    done
    printf '{"domain":"kinetic_voice_router"}\n' > manifest.json
    printf '{"name":"Kinetic Voice Router"}\n' > strings.json
    mkdir -p translations voice_router
    for i in 1 2; do printf 'tr %s\n' "$i" > "translations/de_$i.json"; done
    for i in 1 2 3 4; do printf 'vr %s\n' "$i" > "voice_router/mod_$i.py"; done
    for name in __init__ alarm_engine alarm_session alarm_store dispatch ha_services player scheduler \
                voice_alarm conversation_router intent_parser slots timeparse; do
      printf 'original %s\n' "$name" > "nabu_core/$name.py"
    done
    printf 'package\n' > nabu_core/__init__.py
    while [ "$(find . -type f | wc -l)" -lt 42 ]; do
      printf 'filler\n' > "nabu_core/filler_$RANDOM.py"
    done
  )
  printf 'fake-db\n' > "$root/config/nabu_core/alarms.sqlite"
  printf '{"version":1,"data":{"entries":[]}}\n' > "$root/config/.storage/core.config_entries"
  cat > "$root/bin/ha" <<'HA'
#!/bin/sh
state="$LAB_HA_STATE"
[ -f "$state" ] || echo running > "$state"
case "$1 $2" in
  "core stats")
    if [ "$(cat "$state")" = running ]; then
      printf '{"result":"ok","data":{"memory_usage":123456}}\n'
    else
      printf '{"result":"error","error_key":"homeassistant_not_running_error"}\n'
    fi ;;
  "core stop") echo stopped > "$state"; printf '{"result":"ok"}\n' ;;
  "core start") echo running > "$state"; printf '{"result":"ok"}\n' ;;
  "core logs") printf '2026-11-25 14:00:00 INFO no errors here\n' ;;
  "core info") printf 'version: 2026.11.2\n' ;;
  "supervisor info") printf '{"result":"ok","data":{"healthy":true}}\n' ;;
  *) printf '{"result":"ok"}\n' ;;
esac
HA
  cat > "$root/bin/sha256sum" <<'SHA'
#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    --quiet|--status|-q) echo "sha256sum: unrecognized option: ${arg#--}" >&2; exit 1 ;;
  esac
done
exec /usr/bin/sha256sum "$@"
SHA
  chmod +x "$root/bin/ha" "$root/bin/sha256sum"
  echo running > "$root/ha-state"
}

run_case() {
  local root="$1" installer="${2:-$INSTALLER}"
  LAB_HA_STATE="$root/ha-state" PATH="$root/bin:$PATH" \
    HA_LAB_ROOT="$root" HA_TARGET=root@lab.invalid HA_SSH_KEY=/dev/null \
    bash "$installer" "$STAGE_SRC" > "$root/install.log" 2>&1
  echo "$?"
}

expect() {
  local label="$1" actual="$2" wanted="$3"
  if [ "$actual" = "$wanted" ]; then echo "  PASS  $label ($actual)"; PASS=$((PASS+1));
  else echo "  FAIL  $label: erwartet '$wanted', bekommen '$actual'"; FAIL=$((FAIL+1)); fi
}

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "  PASS  $label"; PASS=$((PASS+1));
  else echo "  FAIL  $label"; FAIL=$((FAIL+1)); fi
}

echo "== Fall A: Erfolgspfad =="
ROOT="$LAB_BASE/a"; prepare_root "$ROOT"
rc=$(run_case "$ROOT")
expect "Exitcode (10 = installiert, unverifiziert)" "$rc" "10"
check "vier Zieldateien getauscht" cmp -s "$ROOT/$CURRENT_REL/nabu_core/player.py" "$STAGE_SRC/nabu_core/player.py"
check "Dateizahl weiterhin 42" test "$(find "$ROOT/$CURRENT_REL" -type f | wc -l)" -eq 42
check "keine .tmp/.r32.new" test -z "$(find "$ROOT/$CURRENT_REL" -name '*.r32.new' -o -name '*.tmp')"
runid=$(grep -o '[0-9]\{8\}-[0-9]\{6\}-[0-9]*' "$ROOT/install.log" | head -1)
check "manifest unberuehrt" cmp -s "$ROOT/$CURRENT_REL/manifest.json" "$ROOT/$BACKUP_REL/$runid/integration/manifest.json"
check "const.py unberuehrt" cmp -s "$ROOT/$CURRENT_REL/const.py" "$ROOT/$BACKUP_REL/$runid/integration/const.py"
check "Rollback-Backup vorhanden" test -d "$ROOT/$BACKUP_REL/$runid/integration"
check "Backup-Hashes vorhanden" test -s "$ROOT/$BACKUP_REL/$runid/SHA256SUMS"
check "Lock mit installed-Marke" test -f "$ROOT/config/backups/nabu-stage-a.lock/installed"
check "Core laeuft wieder" grep -qx running "$ROOT/ha-state"
check "ein Start, ein Stopp" test "$(grep -c 'Core wieder gestartet' "$ROOT/install.log")" -eq 0

echo "== Fall B: Fehler NACH dem Tausch =="
ROOT="$LAB_BASE/b"; prepare_root "$ROOT"
FAILER="$LAB_BASE/install_failverify.sh"
python3 - "$INSTALLER" "$FAILER" <<'PYFAIL'
import pathlib, sys
src = pathlib.Path(sys.argv[1]).read_text()
# Nadel ist die Manifest-Pruefung der Nachpruefung: eindeutig, nicht im Preflight.
needle = "  diff -q '$CURRENT/manifest.json'"
if needle not in src:
    raise SystemExit("Nadel nicht gefunden")
src = src.replace(needle, "  false\n" + needle, 1)
pathlib.Path(sys.argv[2]).write_text(src)
PYFAIL
bash -n "$FAILER" || echo "  (Variantensyntax)"
rc=$(run_case "$ROOT" "$FAILER")
expect "Exitcode (1 = Fehler)" "$rc" "1"
check "Meldung nennt den Tausch" grep -q "Dateien getauscht" "$ROOT/install.log"
check "kein falsches 'Abbruch vor Tausch'" bash -c "! grep -q 'Hinweis: Core nach Abbruch wieder gestartet' '$ROOT/install.log'"
check "Dateien sind getauscht" cmp -s "$ROOT/$CURRENT_REL/nabu_core/scheduler.py" "$STAGE_SRC/nabu_core/scheduler.py"
check "Lock bleibt nach Tausch" test -f "$ROOT/config/backups/nabu-stage-a.lock/owner"

echo "== Fall C: Kopierfehler (kein halber Tausch) =="
ROOT="$LAB_BASE/c"; prepare_root "$ROOT"
chmod 555 "$ROOT/$CURRENT_REL/nabu_core"
rc=$(run_case "$ROOT")
expect "Exitcode (1 = Fehler)" "$rc" "1"
chmod 755 "$ROOT/$CURRENT_REL/nabu_core"
check "Live-Dateien unveraendert" bash -c "grep -qx 'original player' '$ROOT/$CURRENT_REL/nabu_core/player.py'"
check "keine .r32.new-Reste" test -z "$(find "$ROOT/$CURRENT_REL" -name '*.r32.new')"
check "Lock freigegeben" test ! -d "$ROOT/config/backups/nabu-stage-a.lock"
check "Core laeuft wieder" grep -qx running "$ROOT/ha-state"

echo "== Fall D: BusyBox-sha256sum =="
ROOT="$LAB_BASE/d"; prepare_root "$ROOT"
check "Installer nutzt kein --quiet" bash -c "! grep -q 'sha256sum -c --quiet' '$INSTALLER'"
rc=$(run_case "$ROOT")
expect "Exitcode trotz BusyBox-Shim (10)" "$rc" "10"
check "BusyBox-Shim aktiv" test -x "$ROOT/bin/sha256sum"

echo
echo "ERGEBNIS: $PASS bestanden, $FAIL fehlgeschlagen"
[ "$FAIL" -eq 0 ]
