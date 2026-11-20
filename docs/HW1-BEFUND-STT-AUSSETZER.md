# Befund: STT-Aussetzer im Assist-Pfad (eigenständiger Fehler, unabhängig vom Alarm)

Vorgang: `WI0181-NABU-HW1-WAKEWORD-R01`
Stand: 2026-11-20, Wohnzimmer-Echo 2 (`radar`), Firmware `2026.11.18.004106` (unverändert),
Pipeline „Speaker", Cloud-STT über die HA-Assist-Pipeline.
Keine Einstellungen verändert, keine Rohaufzeichnungen gespeichert.

Dieser Befund gehört **nicht** zur Alarmfrage. Er trat ohne jede Wiedergabe auf, im
ruhigen Raum, und würde jede spätere Messung verfälschen, wenn er als „Alarm schuld"
verbucht würde.

## 1. Beobachtung

Reihe R0 (33 Wakeword-Treffer im ruhigen Raum, keine Wiedergabe):

| Reihe | Versuche | Wakeword | VAD-Fenster | Transkript |
|---|---|---|---|---|
| R0 (erste Reihe) | 13 | 13/13 | 0,10–1,21 s | 8/13 |
| R0-A („Stopp") | 10 | 10/10 | 0,56–0,92 s | 8/10 |
| R0-B („wie spät ist es?") | 10 | 10/10 | 1,47–1,85 s | 9/10 |

Das Wakeword ist in allen Reihen vollständig erkannt worden. Verluste entstehen
**ausschließlich** in der Stufe danach.

## 2. Zwei verschiedene Fehlerbilder

**F1 — Fenster zu kurz (VAD schließt vor der Sprache)**
Beispiel 11:34:38.800 → 11:34:38.904: Das VAD-Fenster ist **104 ms** lang, das Transkript
ist leer. Dasselbe bei 11:35:31.809 → 11:35:31.910 (101 ms). Hier war nie Sprache im
Fenster — die Pipeline hat geöffnet und sofort wieder geschlossen.

**F2 — STT antwortet leer, aber spät**
Beispiel 11:42:04.924 → 11:42:05.957 (1,03 s Fenster): Transkript leer, und die
STT-Antwort kommt erst um 11:42:07.992 — **2035 ms** nach Fensterende. Bei allen
erfolgreichen Versuchen derselben Reihe liegt dieselbe Latenz zwischen **35 und 45 ms**.

Zwei Fehlerbilder, zwei Ursachen:
- F1 ist ein Segmentierungsproblem (VAD-Entscheidung), kein Erkennungsproblem.
- F2 ist ein Aussetzer der STT-Stufe selbst — die 50-fache Latenz bei leerem Ergebnis
  ist keine Zufallsschwankung, sondern ein anderer Pfad (Timeout, Wiederholung oder
  Fehlerantwort des Anbieters).

## 3. Status

- **BELEGT:** Beide Fehlerbilder treten ohne Wiedergabe auf; Zeitstempel und
  Fensterlängen liegen im Protokoll.
- **BELEGT:** Die STT-Latenz trennt die beiden Fälle (35–45 ms vs. 2035 ms).
- **HYPOTHESE:** F1 hängt an der VAD-Stille-Schwelle bzw. der Mindestfensterlänge und
  trifft kurze Kommandos („Stopp") härter als ganze Sätze.
- **HYPOTHESE:** F2 ist ein Anbieter-/Netzwerk-Timeout der Cloud-STT; dafür spricht die
  Latenz, dagegen fehlt bisher eine Fehlermeldung im Log.
- **OFFEN:** Welche STT-Engine im Betrieb tatsächlich antwortet (Cloud vs. lokal) — für
  die Ursachensuche von F2 entscheidend, ohne Debug-Log der STT-Komponente nicht belegt.

## 4. Warum das für WI-0181 zählt

Ein Sprachstopp, der am VAD-Fenster oder an einem STT-Aussetzer scheitert, sieht im
Feld genauso aus wie „Wakeword bei Alarm nicht erkannt". Ohne diese Trennung würde die
Alarmreihe die falsche Ursache bestätigen. Deshalb: erst F1/F2 einordnen, dann die
Alarmreihen bewerten.

## 5. Vorgeschlagene Prüfungen (nicht ausgeführt, keine Einstellungsänderung)

1. VAD-Parameter der Pipeline auslesen (nur lesen) und gegen die 0,56–0,92 s der
   Kurzbefehl-Reihe legen.
2. Bei F2 die STT-Komponente für einen Lauf auf `debug` setzen und den Timeout
   mitschneiden — gleiche Vorgehensweise wie beim Pipeline-Log (vorher sichern,
   danach exakt zurück).
3. Zehn Wiederholungen des F2-Falls, um zu sehen, ob die 2-s-Latenz reproduzierbar ist
   oder ein Einzelereignis war.
