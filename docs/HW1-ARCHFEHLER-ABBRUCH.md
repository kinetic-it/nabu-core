# Architekturfehler: Das Wakeword bricht die Alarmwiedergabe ab

Vorgang: `WI0181-NABU-HW1-WAKEWORD-R01`
Stand: 2026-11-20, Wohnzimmer-Echo 2 (`radar`), Firmware `2026.11.18.004106` (unverändert),
keine Firmwareänderung durchgeführt.
Kanal: `media_player.play_media` (Variante A), Datei 30 s, Pegel 0,30.

## 1. Beobachtung

In zehn Versuchen der Reihe P1-30 endete die Wiedergabe in **allen zehn** Fällen innerhalb
von 0–30 ms vor dem `listening`-Ereignis des Satelliten:

| Versuch | Wiedergabe endet | `listening` | Abstand |
|---|---|---|---|
| 1 | 11:52:53.162 | 11:52:53.167 | 5 ms |
| 2 | 11:54:25.366 | 11:54:25.373 | 7 ms |
| 3 | 11:54:47.548 | 11:54:47.551 | 3 ms |
| 4 | 11:55:11.023 | 11:55:11.028 | 5 ms |
| 5 | 11:55:34.233 | 11:55:34.233 | 0 ms |
| 6 | 11:56:05.670 | 11:56:05.677 | 7 ms |
| 7 | 11:56:21.307 | 11:56:21.314 | 7 ms |
| 8 | 11:57:19.754 | 11:57:19.784 | 30 ms |
| 9 | 11:57:40.216 | 11:57:40.229 | 13 ms |
| 10 | 11:58:04.110 | 11:58:04.125 | 15 ms |

Es ist keine Absturzfolge und kein Fehler des Servers: die Datei bleibt abrufbar, HA meldet
`idle`, das Gerät bleibt online, die Pipeline läuft normal weiter.

## 2. Ursache (belegt am Quelltext, nicht am Gerät)

`echo-dot-assist` behandelt jede Assist-Ausgabe als **einen** TTS-Stream. Der Medienjob von
`play_media` und die Antwort der Pipeline teilen sich diesen Strom
(`proto_esphome.c`, `core_tts_begin`/`core_tts_data`; Kommentar in `audio_mixer.c`: Ausgabe
läuft „on the TTS stream through libmixerAPI, so the ln reference stays intact").

Beginnt die Pipeline zu antworten, wird der laufende Medienjob verworfen — im Quelltext als
`job_gone`/`tts_begin refuses` beschrieben. Es gibt **keinen** Mischbetrieb und **keine**
Prioritätsregel, die eine laufende Wiedergabe schützt.

## 3. Warum das ein Architekturfehler ist und kein Schönheitsfehler

1. **Jede Fehlaktivierung reißt den Wecker ab.** Ein falsch verstandenes Wort, ein ähnlich
   klingender Satz aus dem Fernseher, ein Hustenanfall mit passendem Muster — der Alarm
   verstummt. Für ein Gerät, dessen Zweck „zuverlässig klingeln" ist, ist das die
   gefährlichste Einzelregel im ganzen System.
2. **Der Alarm läuft nicht weiter, wenn das Gespräch endet.** Nach der Interaktion bleibt das
   Gerät still. Es gibt niemanden, der die Wiedergabe wieder aufnimmt — außer einer
   übergeordneten Instanz, die davon überhaupt erst erfahren muss.
3. **Der gemessene „Stopp" ist nicht der Stopp.** Was den Alarm beendet, ist das Wakeword,
   nicht der Befehl. Das verdeckt Fehler: eine Erfolgsquote für „Stopp" misst in Wahrheit
   nur, ob das Wakeword angesprungen ist.
4. **Der Ausfall ist nicht symmetrisch.** Musik über den Music-Assistant-Pfad wird gedämpft
   und danach fortgesetzt (`duck.c`); HA-Medien werden verworfen. Für den Nutzer sieht das
   wie ein Zufall aus, technisch ist es eine Sonderbehandlung des Medienkanals.

## 4. Status

- **BELEGT:** In 10/10 Fällen endet `play_media` beim Wakeword; Abstand 0–30 ms.
- **BELEGT:** Die Ursache liegt im geteilten TTS-Stream, dokumentiert im upgestreamten Quelltext.
- **OFFEN:** Ob ein erneutes `play_media` nach der Interaktion zuverlässig wieder klingelt
  (Fortsetzung durch Nabu Core). Muss gemessen werden, nicht angenommen.
- **OFFEN:** Ob ein Assist-Lauf, der keine Antwort erzeugt (z. B. leerer STT-Text), die
  Wiedergabe ebenfalls beendet. Die Fälle 5–10 dieser Reihe deuten darauf hin, sind aber
  nicht sauber getrennt.

## 5. Folgeauftrag (vorgemerkt, nicht ausgeführt)

**Prüfen, ob Nabu Core die Alarmwiedergabe nach einer nicht alarmbezogenen Interaktion
zuverlässig fortsetzen kann.** Konkret zu messen:

1. Alarm starten, Wakeword auslösen, **keinen** Stoppbefehl geben.
2. Nach dem Ende der Pipeline erneut `play_media` auslösen — klingelt es wieder?
3. Wie lange ist die Lücke zwischen Pipeline-Ende und neuem Ton?
4. Bleibt der Alarm stumm, wenn die Interaktion mit einer Fehlermeldung endet?
5. Mehrfach hintereinander: führt jede Interaktion zu einer neuen Lücke, und addiert sich das?

Solange diese Fragen offen sind, darf Variante A nicht als „klingelt zuverlässig" gelten —
unabhängig davon, wie gut die Wakeword-Quote aussieht.
