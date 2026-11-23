"""R21: Sprachabfragen und Weckernamen stabilisieren.

R20-Befunde (2026-11-23, Echo 2, HA 2026.11.2):

* T1: Die Pipeline hoerte "Welche Wecker habe ich?", die Automation bekam
  "Welchewecker habe ich" und der Parser verstand nichts -> HA antwortete "Done".
* T3: "Loesche meinen Wecker pilot" und "Loesche meinen Test. Wecker." endeten in
  einer Rueckfrage bzw. in ``unsupported_tokens``; nur reine ``*wecker``-Komposita
  mit Artikel wurden geloescht.

Die Regeln, die hier festgehalten werden:

* Eine vom Erkenner zusammengezogene Frage ("Welchewecker") ist dieselbe Frage.
* Echte Komposita-Namen ("Medikamentenwecker") bleiben unangetastet.
* Namen werden vor UND nach dem Wort "Wecker" erkannt.
* Ohne Namen wird niemals geloescht - nur nachgefragt.
* Ein Stoppbefehl bleibt ein Stoppbefehl (keine Regression von R18/R19.1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from nabu_core.voice_alarm import AlarmParser

TZ = ZoneInfo("Europe/Berlin")


def parse(utterance: str):
    return AlarmParser(timezone=TZ).parse(utterance, now=datetime.now(UTC))


def kind_of(result) -> str | None:
    intent = getattr(result, "intent", None)
    kind = getattr(intent, "kind", None)
    return getattr(kind, "value", kind)


def status_of(result) -> str:
    return str(getattr(result.status, "value", result.status))


@pytest.mark.parametrize(
    "utterance",
    [
        "Welche Wecker habe ich?",
        "Welchewecker habe ich",
        "welchewecker habe ich noch",
        "welche wecker sind aktiv",
        "Was für Wecker habe ich",
        "zeige mir meine wecker",
    ],
)
def test_glued_and_spaced_list_questions_are_lists(utterance: str) -> None:
    result = parse(utterance)
    assert status_of(result) == "ok", result
    assert kind_of(result) == "list"


@pytest.mark.parametrize(
    "utterance,expected_label",
    [
        ("Lösche meinen Wecker pilot", "pilot"),
        ("Lösche den Wecker Testwecker", "testwecker"),
        ("Lösche Testwecker", "testwecker"),
        ("Lösche meinen Test. Wecker.", "test"),
        ("entferne meinen wecker zahnarzt", "zahnarzt"),
    ],
)
def test_delete_understands_split_and_trailing_names(
    utterance: str, expected_label: str
) -> None:
    result = parse(utterance)
    assert status_of(result) == "ok", result
    assert kind_of(result) == "delete"
    assert result.intent.label == expected_label


@pytest.mark.parametrize(
    "utterance",
    ["Lösche meinen Wecker", "Lösche den Wecker", "Lösche Wecker"],
)
def test_delete_without_a_name_only_asks(utterance: str) -> None:
    """Never guess which alarm is meant - a wrong deletion is unrecoverable."""
    result = parse(utterance)
    assert status_of(result) == "clarification"
    assert result.code == "missing_alarm_selector"
    assert result.intent is None


def test_compound_names_are_not_split_apart() -> None:
    """A real compound name must survive the de-merging step untouched."""
    result = parse("Lösche meinen Medikamentenwecker")
    assert status_of(result) == "ok", result
    assert result.intent.label == "medikamentenwecker"


@pytest.mark.parametrize(
    "utterance,kind",
    [
        ("Stopp.", "stop"),
        ("Hör auf", "stop"),
        ("Wecker aus", "stop"),
        ("Klingel aus", "stop"),
        ("Nabu stopp", "stop"),
    ],
)
def test_stop_phrases_still_win(utterance: str, kind: str) -> None:
    """R18/R19.1 must not regress: a stop stays a stop."""
    result = parse(utterance)
    assert status_of(result) == "ok", result
    assert kind_of(result) == kind


@pytest.mark.parametrize(
    "utterance",
    ["was für ein Gerät ist das", "wie viele wecker gibt es"],
)
def test_underspecified_input_is_not_a_list(utterance: str) -> None:
    """Do not turn a fragment into a list answer."""
    result = parse(utterance)
    assert kind_of(result) != "list"
