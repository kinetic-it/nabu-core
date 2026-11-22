"""R18: Ein Stopp muss auch mit Fuellwoertern und Satzzeichen greifen.

Hardware-Befund R17 (2026-11-22, Test 5): Waehrend eines laufenden Rings
transkribierte die Spracherkennung "Oh, Stopp." und spaeter "Stopp.". Keiner der
beiden Saetze erreichte den Alarm, der Wecker lief bis zum Ablauf des
60-Sekunden-Fensters weiter.

Gleichzeitig darf ein alarmfremder Satz den Wecker niemals beenden - deshalb
muss der *ganze* Satz ein Stoppbefehl sein, nicht nur ein Wort darin.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from nabu_core.voice_alarm import (
    AlarmParser,
    IntentKind,
    ParseStatus,
    is_generic_stop,
    is_stop_utterance,
    match_stop,
)

MOMENT = datetime(2026, 11, 22, 16, 25, tzinfo=UTC)
PARSER = AlarmParser(timezone=ZoneInfo("Europe/Berlin"))

GENERIC_STOPS = [
    "Stopp",
    "Stopp.",
    "stopp!",
    "Oh, Stopp",
    "Oh, Stopp.",
    "Okay, stopp bitte",
    "Ähm stopp",
    "halt",
    "Ruhe!",
    "Aus.",
    "stopp doch mal jetzt",
    "Genug jetzt",
]

NAMED_STOPS = [
    "Wecker aus",
    "Wecker aus.",
    "Stopp den Wecker",
    "Hör auf",
    "Hör auf!",
    "hör auf zu klingeln",
    "Aufhören",
    "sei still",
    "Alarm aus",
    "Mach den Wecker aus.",
    "hey nabu stopp",
]

FOREIGN_SPEECH = [
    "Was ist das für ein Geräusch?",
    "Wie wird das Wetter morgen?",
    "Ich muss aufhören zu rauchen",
    "Wie stoppe ich den Wecker morgen?",
    "Stelle einen Wecker auf 6 Uhr",
    "Hör nicht auf",
    "Stopp nicht",
    "Bitte nicht stoppen",
    "Wie viel Uhr ist es?",
    "Hör auf zu singen",
]


@pytest.mark.parametrize("utterance", GENERIC_STOPS)
def test_bare_stop_is_recognised(utterance: str) -> None:
    assert match_stop(utterance) == "generic", utterance
    assert is_generic_stop(utterance) is True
    assert is_stop_utterance(utterance) is True


@pytest.mark.parametrize("utterance", NAMED_STOPS)
def test_named_stop_is_recognised(utterance: str) -> None:
    assert match_stop(utterance) == "alarm", utterance
    assert is_generic_stop(utterance) is False
    assert is_stop_utterance(utterance) is True


@pytest.mark.parametrize("utterance", [*GENERIC_STOPS, *NAMED_STOPS])
def test_parser_turns_every_stop_into_a_stop_intent(utterance: str) -> None:
    parsed = PARSER.parse(utterance, now=MOMENT)

    assert parsed.status is ParseStatus.OK, (utterance, parsed.code)
    assert parsed.intent is not None
    assert parsed.intent.kind is IntentKind.STOP


@pytest.mark.parametrize("utterance", FOREIGN_SPEECH)
def test_unrelated_speech_never_becomes_a_stop(utterance: str) -> None:
    assert match_stop(utterance) is None, utterance
    assert is_stop_utterance(utterance) is False

    parsed = PARSER.parse(utterance, now=MOMENT)
    if parsed.intent is not None:
        assert parsed.intent.kind is not IntentKind.STOP, utterance


def test_negation_is_never_read_as_stop() -> None:
    """ "Hoer NICHT auf" heisst weiterklingeln, nicht aufhoeren."""

    assert match_stop("Hör nicht auf") is None
    assert match_stop("Stopp nicht") is None
    assert match_stop("Bitte nicht stoppen") is None


def test_the_r17_hardware_transcript_is_a_stop() -> None:
    """Der genaue Satz aus dem fehlgeschlagenen Abnahmetest."""

    assert match_stop("Oh, Stopp.") == "generic"


def test_stop_matching_has_no_state_and_is_deterministic() -> None:
    for _ in range(3):
        assert match_stop("Oh, Stopp.") == "generic"
        assert match_stop("Wecker aus.") == "alarm"
        assert match_stop("Hör auf zu singen") is None
