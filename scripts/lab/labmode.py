"""Fügt dem Installer einen Labormodus (HA_LAB_ROOT) hinzu und ersetzt SSH-Aufrufe.

Der Labormodus laesst dieselben Remote-Skriptblöcke gegen ein temporaeres
Wurzelverzeichnis laufen: '/config' wird auf '$HA_LAB_ROOT/config' abgebildet,
der SSH-Aufruf wird zu 'sh -c'. Damit laesst sich das Installationsverfahren
(BusyBox-Verhalten, Pfade, zweistufiger Tausch, Zustandserkennung) ohne
produktive Installation pruefen.
"""

from __future__ import annotations

import pathlib
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
text = source.read_text()

anchor = 'SSH=(timeout --signal=TERM --kill-after=5 "${SSH_TIMEOUT}s" ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -i "$HA_SSH_KEY" "$HA_TARGET")\n'
if anchor not in text:
    raise SystemExit("Ankerstelle fuer die SSH-Definition nicht gefunden")

lab = anchor + """
# Labormodus: HA_LAB_ROOT zeigt auf eine Wegwerf-Wurzel. Dann laufen die
# Remote-Bloecke lokal unter sh -c und '/config' wird umgeschrieben. Produktiv
# bleibt dieser Zweig unbenutzt.
ssh_run() {
  if [ -n "${HA_LAB_ROOT:-}" ]; then
    local cmd
    cmd=$(printf '%s' "$1" | sed "s#/config#${HA_LAB_ROOT}/config#g")
    sh -c "$cmd"
  else
    "${SSH[@]}" "$1"
  fi
}
"""
text = text.replace(anchor, lab)

before = text.count('"${SSH[@]}"')
text = text.replace('"${SSH[@]}"', "ssh_run")
after = text.count('"${SSH[@]}"')
target.write_text(text)
print(f"SSH-Aufrufe ersetzt: {before} -> {after} offen")
print(f"ssh_run-Aufrufe: {text.count('ssh_run')}")
