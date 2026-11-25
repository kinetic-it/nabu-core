# WI-0181 R32 – Ducking und Klingelsteuerung stabilisieren (25.11.2026)

**Vorgang:** WI0181-R32-DUCKING-20261125
**Repository:** `kinetic-it/nabu-core`
**Branch:** `wi0181-r32-ducking-20261125`
**Commits:** `780b62b` (Kern), `82ec7a9` (Werkzeug)
**Basis:** `fe4448d` (R31-Teststand), Live-Referenz `ba0b008`
**Art:** Entwicklungs-/Laborarbeit. Kein Deployment, keine HA-Änderung, keine hörbaren Tests.

## 1. Nachgewiesene Ursachen

| Ursache | Beleg |
|---|---|
| **Ducking auf 0.0 beendet am Echo 2 die Wiedergabe** (`playing` → `idle`) | R31-Hardwaremessung: Absenkfenster 0,099–0,167 s, danach 6,7 s Stille. Im Labor nachgestellt (`test_zero_volume_ends_the_playback_on_this_device`). |
| **`NOT_RINGING` zwischen Ring-Generationen** | `SerialDispatcher.ring()` löscht `_target` im `finally`; `duck()` verlangte dieses Feld. Während der Restart-/Interaktionspause lebt kein Ring, obwohl die Sitzung den Lautsprecher besitzt. R31-Log: `alarm_duck → ok:false, code=NOT_RINGING` bei hörbarem Klingeln. |
| **Fehlender Stopp-Nachweis** bei abgebrochenem Turn | Labortest bricht `Scheduler.stop()` mitten in der Bestätigung ab: `Nabu stop: enter` + Fence, **kein** `exit`, keine Fehlerzeile — identisch zum R31-Gerätebild. |
| **Restart-Kaskade** | Live-Medium ist `nabu_audio_test_2s.wav` (2 s) bei `alarm_cycle_on=4`/`alarm_cycle_off=3`. Jede 0.0-Phase stoppt den Player, jede Rückkehr startet neu; zusätzlich endet die 2-s-Datei alle 2 s. |

## 2. Gewählte Lösung und Begründung

1. **Duck-Floor statt 0.0** (`duck_floor`, Default 0.05, validiert `< Ringpegel`). Der Ring bleibt hörbar gedämpft statt zu verstummen; das Fenster wird wie bisher nach Ablauf auf den Ringpegel zurückgeführt. Grund: 0.0 ist auf diesem Gerät kein Mute, sondern ein Stopp — die Gerätesemantik wird respektiert statt überstimmt.
2. **Duck-Fenster über Generationsgrenzen** (`PlaybackStatus.DUCK_DEFERRED`). Der Dispatcher hält ein Sitzungsziel, das nur ein Stopp löscht; der Adapter merkt das Fenster vor und wendet es auf die nächste Generation **desselben** Ziels an, startet diese direkt geduckt und verwirft es bei Ablauf, bei fremdem Ziel und am Sitzungsende. Grund: ein Wakeword in der Pause darf NABU nicht taub machen.
3. **Stopp-Nachweis entkoppelt.** Fence (`_persist_stop`) bleibt unverändert; die Bestätigung läuft als eigener, gehaltener Task mit `finally`-Log (`cancelled=true/false`). Ein abgebrochener Assist-Turn verhindert weder die Markierung `stop_confirmed` noch den Logeintrag. Cancellation wird nicht unterdrückt — sie wird nur nicht mehr zum Datenverlust.
4. **Klingelrhythmus aus der Datei.** `tools/build_ring_loop.py` erzeugt aus dem vorhandenen Ton eine Schleife (Ton unverändert wiederholt + digitale Stille). Labordatei aus `/media/nabu_audio_test_2s.wav`: **7,000 s**, 16 kHz mono s16le, 4 s Ton + 3 s Stille, `sha256 356895cb618993c8f06e79725b341b6f56087feeb53180808c1c3353fd04d56e`; Quelle `sha256 ddcb6330…`, Ton-Block byte-identisch zur Quelle, Stille nachweislich 96 000 Nullbytes. Damit sind für den Rhythmus **keine** Pegelschreibvorgänge mehr nötig (Test: `client.writes == []`).

## 3. Verworfene Alternativen

- **`volume_mute` als Duck**: nicht belegt, dass dieses Gerät dabei die Wiedergabe hält; ohne Hardwaretest wäre es derselbe Fehler mit anderem Namen. Als Option für die Hardwareabnahme offen.
- **Absenkung auf `volume_min` des Geräts**: Wert unbekannt, kein Auslesen ohne Hardwareversuch. Der Floor ist deshalb konfigurierbar, nicht fest verdrahtet.
- **Turn-Abbruch global unterdrücken** (`except BaseException: pass`): versteckt Fehler und hält HA-Turns offen — verworfen.
- **Eigene Wiederholung im Adapter für die Pausen**: erzeugt genau die Stopp-/Neustart-Kaskade, die beseitigt werden soll.
- **Pipeline-Neustarts gegen den leeren Transkript-Fall**: ungeprüfte Workarounds sind ausgeschlossen; upstream offen (HA #191712 / #186694).

## 4. Geänderte Dateien

| Datei | Änderung |
|---|---|
| `nabu_core/player.py` | `duck_floor`, `DUCK_DEFERRED`, Duck schreibt den Floor, Duck-Intent über Generationen, Aufräumen des Intents |
| `nabu_core/dispatch.py` | Sitzungsziel (`_session_target`) überlebt Generationen, wird vom Stopp gelöscht; `duck()` reicht das Ziel durch |
| `nabu_core/scheduler.py` | `_stop_and_confirm` als eigener Task, `finally`-Log mit `cancelled`-Flag |
| `nabu_core/ha_services.py` | `DUCK_SCHEDULED` (ok, aber nicht hörbar) statt Fehlercode |
| `tests/nabu_core/test_r32_duck_floor.py` | 5 Tests: Gerätesemantik, Floor, Rückkehr, manueller Pegel, Validierung |
| `tests/nabu_core/test_r32_duck_intent.py` | 4 Tests: Vormerken, Ablauf, kein Duck nach Stopp, kein Zielwechsel |
| `tests/nabu_core/test_r32_stop_cancel.py` | 1 Test: Stopp-Nachweis überlebt abgebrochenen Turn |
| `tests/nabu_core/test_r32_ring_cadence.py` | 2 Tests: dateibasierter Rhythmus ohne Pegelschreiben, Negativkontrolle zum alten Zyklus |
| `tests/nabu_core/test_alarm_stability.py`, `test_r30_ducking_scope.py`, `tests/ha/test_alarm_wiring.py` | Erwartungen von 0.0 auf den Floor nachgezogen (Verhalten absichtlich geändert) |
| `tools/build_ring_loop.py` | Schleifenwerkzeug, reproduzierbar |

## 5. Testergebnisse

- **RED vor GREEN**: `test_stop_confirmation_and_log_survive_a_cancelled_turn` scheitert auf `fe4448d` (nur `enter`, kein `exit`), mit dem Fix grün.
- `pytest tests`: **1185 passed** (1173 + 12 neue).
- `bundle_integration.py --check`: up to date; zwei Builds → identischer Baum-Hash `d7adc20aa44ca2ea7ad3396eb6b549a8553a908633141b5ab84ca26b0813f6d1`.
- Ruff `check` und `format --check` auf allen geänderten Dateien grün.

## 6. Restrisiken

- Der Floor 0.05 ist ein Laborwert; ob er am Echo 2 hörbar leise **und** für die Spracherkennung ausreichend ist, entscheidet erst die Hardwareabnahme.
- Nach einer manuellen Lautstärkeänderung während des Klingelns duckt die Sitzung bewusst nicht mehr (Eigentum wird freigegeben). Offener Produktpunkt, kein Fehler.
- Der dateibasierte Rhythmus ist im Labor belegt, aber die Live-Konfiguration nutzt weiterhin 2 s + Pegelzyklus. Umschalten erst nach Abnahme.
- Divergente Git-Historien: `wi0181-*`-Zweige haben **keine** gemeinsame Historie mit `origin/main` (Wurzeln `8a4585eb` vs. `0730644a`). Ein PR gegen `main` ist deshalb nicht möglich; die Zusammenführung ist eine Owner-Entscheidung (kein Rebase/Merge durch Nexis).

## 7. D5-Status

**Offen und unverändert.** Der Player meldet weiterhin keine Medien-ID (`IDENTITY_UNKNOWN`). Die neuen Pfade ducken nur Ziele, auf denen diese Sitzung selbst nachweislich abgespielt hat (`_recent_effect`, Sitzungsziel), und niemals fremde Wiedergabe. Ein Nachweis fremder Medienidentität am Gerät fehlt weiterhin.

## 8. Hardware-Abnahmeplan (Vorschlag, nach Freigabe)

Voraussetzung je Test: Owner anwesend, Echo 2 frei, Lautstärke 0.3, keine fremde Wiedergabe.

1. **Floor-Hörbarkeit**: Wecker klingeln, „Okay Nabu“ sprechen. Erwartet: Pegel 0.3 → 0.05 für ~12 s, Klingeln **läuft weiter** (kein `idle`), danach zurück auf 0.3, Antwort hörbar.
2. **Duck über Generationsgrenze**: Wakeword gezielt in die Pause legen (Sekunde 5–6 nach Klingelstart). Erwartet: kein `NOT_RINGING` mehr, nächste Generation startet geduckt (`DUCK_SCHEDULED` → hörbar leise).
3. **Stopp-Nachweis**: „Okay Nabu, Stopp“. Erwartet: `Nabu stop: exit confirmed=true` **und** `stop_confirmed` im Journal, auch wenn die Antwort abgeschnitten wird.
4. **Dateibasierter Rhythmus**: nach Umstellung auf die 7-s-Datei (`alarm_cycle_on=0`, `alarm_cycle_off=0`): 4 s an / 3 s aus hörbar, keine Pegel-Schreibvorgänge im Log, deutlich weniger Neustarts.
5. **Manueller Pegel**: während des Klingelns lauter stellen. Erwartet: Wert bleibt, Duck unterbleibt (dokumentierte Grenze).

## 9. Installationsfreigabe — Anfrage

Für einen Test am Gerät wären nötig:

- Bundle auf HA VM200 (`/config/custom_components/kinetic_voice_router`, 42 Dateien → neuer Stand),
- neue Mediendatei `nabu_audio_loop7s.wav` unter `/media/`,
- Optionsänderung `alarm_media` + `alarm_cycle_on=0`, `alarm_cycle_off=0`, optional `alarm_duck_floor`.

**Nichts davon ist ausgeführt.** Für Schritt 1–3 genügt Code + Neustart der Integration; Schritt 4 braucht zusätzlich die Datei und die Optionsänderung. Rückweg: vorheriger Live-Stand `ba0b008` (42/42-Hashnachweis liegt vor).
