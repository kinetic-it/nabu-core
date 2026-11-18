# Nabu Core: experimentelles Alarm-Fundament

**Nicht produktiv einsetzen.** Dieses Dokument beschreibt einen Laborstand, keinen
Release und keine zugesicherte Weckfunktion. Die bestehende Integration
`kinetic_voice_router` bleibt unverändert; der Nachweis der späteren Kompatibilität
ist noch offen. Das bisherige Repository bleibt bestehen.

## Bestandteile

- `alarm_engine.py`: zehn stabile Plätze, persistente UUID je Belegung, SQLite als
  einzige Autorität für Weckerdefinitionen, Transaktionen und Namenprüfung.
- `recurrence.py`: Wochenzeit-Berechnung. Mehrdeutige oder nicht vorhandene lokale
  Uhrzeiten bei Zeitumstellungen werden explizit als Fehler behandelt.
- `scheduler.py`: vom Host aufzurufender Fälligkeitsschritt und persistente
  Occurrence-Zustände. Kein autonom laufender Dienst.
- `dispatch.py`: serieller Kanal mit expliziter Ziel-Allowlist; ein Player wird
  injiziert. Im Labor ausschließlich Fake-Player.
- `voice_alarm.py` / `api.py`: streng begrenzter deutscher Parser und In-Process-
  Schnittstelle für Verwaltung und Sprache.

Die bestehende Router-Integration gehört zum Ursprungscode und bleibt an ihrer
ursprünglichen Stelle. Ein Umbenennen der HA-Domain oder produktive Migration
wird durch diesen Laborstand nicht vorgenommen. Timer, Erinnerungen und Gedächtnis
sind nicht implementiert. Hermes ist kein Weckerspeicher.

## Schnittstellen und Fehler

`AlarmService` stellt `create`, `get`, `list`, `update`, `enable`, `disable`
und `delete` bereit. Ergebnisse tragen `code` und gegebenenfalls `alarm`,
`alarms` und `question`. Domainfehler umfassen `NO_FREE_SLOT`, `NAME_CONFLICT`,
`NOT_FOUND`, `INVALID_INPUT` und `DATABASE_INCOMPATIBLE`; Speicherfehler werden
als Fehler zurückgegeben. `VoiceAlarmAdapter.handle` liefert bei Mehrdeutigkeit
eine Rückfrage und bei Ablehnung keine Erfolgsbestätigung. Eine
Stop-Anforderung ist kein Nachweis physischer Stille.

Die Schnittstelle ist **nur In-Process**: Kein HTTP-Server, keine Authentisierung,
kein Dashboard, keine HA-Anbindung und keine echte Sprachausgabe sind hier
implementiert. Eine spätere Oberfläche und HA benötigen geeignete authentisierte
Adapter, die dieselbe Autorität nutzen. Die experimentelle SQLite-Datei ist an
die konfigurierte Zeitzone und das Schema gebunden; automatische Migration
bestehender Entwicklungsdaten ist nicht implementiert.

## Offene Sicherheits- und Betriebsgrenzen

Reale Geräte, Geräteidentitäten und die explizite Geräte-Ausschlussliste sind
nicht integriert oder getestet. Der Playervertrag muss insbesondere ausstehende
und bereits übertragene Medienbefehle sowie Abbruchbestätigungen unterscheiden.
Ein Commit eines Abbruchs kann den physisch bereits gesendeten Befehl nicht
rückwirkend verhindern. Weder Verfügbarkeit des Players noch echte akustische
Stille, Crash-/Stromausfallsicherheit oder längerfristiger Betrieb sind belegt.
Vor einer Produktivfreigabe müssen auch Restart-Recovery, Nachholpolitik,
Zeitumstellungen, Konkurrenz, Berechtigungen und vollständige Router-/HA-
Integration mit negativen und End-to-End-Tests nachgewiesen werden.

Die Upstream-Historie ist lokal enthalten. Die Übernahme in ein öffentliches
Zielrepository bleibt bis zum vollständigen Public-Audit einschließlich aller
Historienobjekte, Lizenz- und Datenschutzprüfung gesperrt. Frühere
Repository-Ablösung nur nach separater Governance-Freigabe.
