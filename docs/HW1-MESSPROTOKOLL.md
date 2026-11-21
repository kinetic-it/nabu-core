# [internal-issue] · H-W1 — Messprotokoll Wakeword während des Klingelns

Vorgang: `WI0181-NABU-HW1-WAKEWORD-R01`
Gerät: Wohnzimmer-Echo 2 (`radar`, `[private-address]`), Firmware `2026.11.18.004106`, unverändert.
Arbeitsbaum: `/home/[private-host]/work/nabu-core-r07a` ([private-host] [private-host], `[private-host]`).
Küchendot, R6 und P0 sind ausgeschlossen.

Ziel: getrennt messen, wie oft (1) das Wakeword erkannt wird, (2) die Pipeline startet,
(3) ein Transkript entsteht, (4) der Stopp gelingt — und wie lange es bis zur Stille dauert.
**Keine dieser vier Stufen wird mit einer anderen gleichgesetzt.**

---

## 1. Entitäten und Kanäle

| Zweck | Entität |
|---|---|
| Wakeword/Pipeline | `assist_satellite.echo_2_c2709b_assist_satellit` |
| Wiedergabe, Lautstärke | `media_player.echo_2_c2709b_media_player` |
| Firmwarestand | `update.echo_2_c2709b_firmware` |
| Auto-Update (muss aus bleiben) | `automation.nabu_echo_2_stable_auto_update` |

Beobachtete Zustände des Satelliten: `idle` → `listening` (Wakeword/Pipeline) →
`processing` (STT/Intent) → `responding` → `idle`.

**Beweisgrenze:** Der Zustandswechsel nach `listening` belegt, dass das Gerät das Wakeword
erkannt und die Pipeline gestartet hat. Er belegt **nicht**, dass ein Transkript entstand und
**nicht**, dass der Befehl ausgeführt wurde. Transkripte stehen nur bei aktiviertem Debug-Log
zur Verfügung; das ist eine eigene, reversible Entscheidung (siehe Abschnitt 7).

## 2. Klingeldatei und Auslieferung

- Datei: `assets/hw1/bell-4-3.wav` — 30 s, 4 s Ton / 3 s Pause, 880 Hz sinus, 48 kHz mono,
  erzeugt mit `tools/hw1/make_bell.py` (keine Sprache, damit STT nichts als Befehl hört).
- Auslieferung: `tools/hw1/serve_alarm.py` auf [private-host] (`[private-address]:8085`), Protokoll
  jeder Anfrage in `hw1-fetch.jsonl`.
- Warum überhaupt ein eigener Server: Das Gerät holt die Mediendatei selbst. Der Zugriffslog
  ist der einzige harte Beleg, dass der Echo die Datei wirklich geladen hat — „HA hat
  akzeptiert“ ist kein Beleg.
- Alternativ (falls der Zugriff scheitert): Datei in HA `www/` legen (`/local/…`).

## 3. Reihenfolge der Reihen

| Reihe | Bedingung | Versuche | Zweck |
|---|---|---|---|
| S0 | Stille Probe, Lautstärke 0.0 | 1–2 | Beweist Abruf und Wiedergabekette ohne Ton |
| R0 | Referenz ohne Audio | 10 | Wakeword-Grundlage im Raum |
| P1-30 | `play_media`, 30 % | 10 | Hauptreihe |
| P1-60 | `play_media`, 60 % | 10 | nur nach Zwischenkontrolle |
| P1-100 | `play_media`, 100 % | 10 | nur auf Entscheidung |
| P3 | Music-Assistant-Stream | — | **derzeit nicht verfügbar**: in HA existiert kein MA-Media-Player (nur Echo-2, KüchenDot, Cube, Attilas Fire, Überall) |

Zwischen den Reihen: Abbruchkriterien prüfen, Lautstärke kontrollieren, Ergebnis ansehen.
Kein automatischer Beginn der nächsten Reihe.

## 4. Ablauf je Versuch (beaufsichtigt)

1. Vorzustand festhalten: Satellit `idle`, Medienplayer `idle`, Lautstärke, kein fremder Stream.
2. Server läuft, Datei erreichbar (S0 einmal bestätigt).
3. Alarm starten: `media_player.play_media` mit `http://[private-address]:8085/bell-4-3.wav`,
   Inhaltstyp `music`, Lautstärke der Reihe.
4. Arusha spricht **„Okay Nabu“**, danach **„Stopp“**.
5. Nach spätestens 25 s: `media_player.media_stop`, Lautstärke zurück auf Vorzustand.
6. Nachzustand prüfen und den Versuch als Zeile protokollieren.

Zeitmarken kommen aus der HA-Historie (`ha_get_history`) der beiden Entitäten:
Beginn der Wiedergabe, Satellit `listening`, `processing`, `responding`, Ende der Wiedergabe.
„Zeit bis zur Stille“ = Ende der Wiedergabe − Zeitpunkt des Stoppbefehls.

## 5. Protokollzeile

```
tools/hw1/protokoll.py add --file hw1.jsonl --reihe P1-30 --versuch 3 \
  --wakeword ja --pipeline ja --transkript unklar --stopp ja \
  --stille-s 1.8 --falschaktivierung nein --wiederhergestellt ja --notiz ""
```

Felder: `wakeword`, `pipeline`, `transkript`, `stopp` (je `ja|nein|unklar`), `stille_s`,
`falschaktivierung`, `wiederhergestellt`, `notiz`. Auswertung: `protokoll.py report`.

## 6. Abbruchkriterien (sofort)

- Alarm lässt sich nicht stoppen (zwei Stoppversuche ohne Wirkung).
- Ungewöhnliche Lautstärke (lauter als eingestellt, Sprung auf 100 %).
- Fremde Wiedergabe betroffen (KüchenDot, Cube, Überall, Attilas Fire ändern ihren Zustand).
- Gerät nicht mehr erreichbar oder HA-Anbindung weg.
- Satellit bleibt in `listening`/`processing` hängen.

**Manueller Notstopp** (in dieser Reihenfolge):
1. `media_player.media_stop` auf `media_player.echo_2_c2709b_media_player`
2. `media_player.volume_set` auf den Ausgangswert
3. Server auf [private-host] beenden (`Ctrl-C` bzw. Prozess beenden)
4. Wenn HA nicht mehr reagiert: `button.echo_2_c2709b_restart` (Geräteneustart, keine Firmwareänderung)

## 7. Offene Punkte vor dem ersten Ton

1. **S0 muss zuerst bestätigt sein** (Abruf im Serverlog, Lautstärke 0.0 — kein Ton).
2. **Transkript-Nachweis** ist ohne Debug-Log nicht möglich. Vorschlag: `assist_pipeline`
   temporär auf `debug` (`ha_set_integration(log_level=...)`), nach der Reihe zurück auf
   `warning`. Das ist eine HA-Einstellung, keine Firmware — Entscheidung liegt beim Owner.
3. **P3 (MA)** ist derzeit nicht verfügbar; die Reihe entfällt, bis ein MA-Player existiert.

## 8. Rückbau

- Lautstärke nach jedem Versuch auf den Ausgangswert (`0.5` bei Beginn der Messung).
- Kein Firmwareeingriff, kein OTA, kein R6.
- Auto-Update-Automatik bleibt aus; sie wird erst nach Abschluss der Messungen bewusst
  entschieden.
- Server und Protokolldateien liegen unter [private-host]/`tools`+`assets`; Löschen genügt.
