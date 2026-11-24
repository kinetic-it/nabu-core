"""R26 regression: leading fillers, alarm name forms and the alarm-command gate.

R26 shipped three fixes that had no test of their own. Before R26,

* "Äh, welche Wecker habe ich?" ended as ``unsupported_utterance``,
* "Lösche Wecker Arbeit" was not understood (only the glued form was), and
* an utterance the grammar did not own made the router answer ``None``, so
  "Wecker auf 7 Uhr" was answered by the chat agent.

These tests pin the documented behaviour including the counter-examples that
must keep behaving as before: a negation never acts, the last word is never
stripped, an incomplete command asks back and trailing junk is refused.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nabu_core.voice_alarm import (
    HINT_COMMAND,
    HINT_LIST,
    HINT_NEGATED,
    AlarmParser,
    IntentKind,
    ParseStatus,
    _strip_leading_fillers,
    alarm_command_hint,
    is_alarm_command,
)

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 3, 18, 12, 30, tzinfo=BERLIN)  # a Wednesday


def parse(utterance: str):  # noqa: ANN201 - test helper
    return AlarmParser(timezone=BERLIN).parse(utterance, now=NOW)


# --- Aufgabe 1: fuehrende Fuellwoerter ---------------------------------------
@pytest.mark.parametrize(
    ("utterance", "kind"),
    [
        ("Äh, welche Wecker habe ich?", IntentKind.LIST),
        ("Also, welche Wecker habe ich?", IntentKind.LIST),
        ("Hallo, welche Wecker habe ich?", IntentKind.LIST),
        ("Ähm, stell einen Wecker auf 7 Uhr.", IntentKind.CREATE),
        ("Oh, Stopp.", IntentKind.STOP),
    ],
)
def test_a_leading_filler_does_not_break_the_command(utterance: str, kind: IntentKind) -> None:
    result = parse(utterance)

    assert result.status is ParseStatus.OK, f"{utterance!r} was rejected as {result.code!r}"
    assert result.intent is not None
    assert result.intent.kind is kind


@pytest.mark.parametrize("utterance", ["Äh", "Äh.", "Hmm", "Also"])
def test_a_filler_on_its_own_stays_unparsed(utterance: str) -> None:
    """The last remaining word is never stripped, so nothing "becomes" a command."""

    result = parse(utterance)

    assert result.status is ParseStatus.ERROR
    assert result.code == "unsupported_utterance"


def test_the_helper_never_strips_the_last_word() -> None:
    assert _strip_leading_fillers([]) == []
    assert _strip_leading_fillers(["äh"]) == ["äh"]
    assert _strip_leading_fillers(["oh", "stopp"]) == ["stopp"]
    assert _strip_leading_fillers(["äh", "also", "wecker"]) == ["wecker"]
    assert _strip_leading_fillers(["wecker", "aus"]) == ["wecker", "aus"]


@pytest.mark.parametrize(
    "utterance",
    [
        "Nein, lösche meinen Wecker Arbeit.",
        "Nicht lösche meinen Wecker Arbeit.",
        "Äh, nein, lösche meinen Wecker Arbeit.",
    ],
)
def test_a_negated_command_never_acts(utterance: str) -> None:
    """Negations are deliberately absent from ``_LEADING_FILLERS``."""

    result = parse(utterance)

    assert result.status is ParseStatus.ERROR
    assert result.intent is None


def test_a_negated_command_is_answered_without_changing_anything() -> None:
    assert alarm_command_hint("Nein, lösche meinen Wecker Arbeit") == HINT_NEGATED
    assert is_alarm_command("Nein, lösche meinen Wecker Arbeit") is False


# --- Aufgabe 2: getrennte Weckernamen ---------------------------------------
@pytest.mark.parametrize(
    ("utterance", "kind", "label"),
    [
        ("Lösche Wecker Arbeit.", IntentKind.DELETE, "arbeit"),
        ("Lösche den Wecker Arbeit.", IntentKind.DELETE, "arbeit"),
        ("Lösche Wecker Arbeit", IntentKind.DELETE, "arbeit"),
        ("Aktiviere den Wecker Arbeit.", IntentKind.ACTIVATE, "arbeit"),
        ("Deaktiviere meinen Wecker Arbeit.", IntentKind.DEACTIVATE, "arbeit"),
        ("Lösche meinen Arbeitwecker.", IntentKind.DELETE, "arbeitwecker"),
        ("Lösche Arbeit Wecker.", IntentKind.DELETE, "arbeit"),
    ],
)
def test_the_alarm_name_may_stand_before_or_after_the_word_wecker(
    utterance: str, kind: IntentKind, label: str
) -> None:
    result = parse(utterance)

    assert result.status is ParseStatus.OK, f"{utterance!r} was rejected as {result.code!r}"
    assert result.intent is not None
    assert result.intent.kind is kind
    assert result.intent.label == label


def test_a_missing_alarm_name_asks_back_instead_of_guessing() -> None:
    result = parse("Lösche Wecker.")

    assert result.status is ParseStatus.CLARIFICATION
    assert result.code == "missing_alarm_selector"
    assert result.intent is None
    assert result.question == "Welchen Wecker soll ich löschen?"


@pytest.mark.parametrize(
    "utterance",
    ["Lösche Wecker Arbeit bitte jetzt sofort.", "Lösche Wecker Arbeit und Morgen"],
)
def test_trailing_words_are_still_refused(utterance: str) -> None:
    """A wider name pattern must not turn into "guess the rest"."""

    result = parse(utterance)

    assert result.status is ParseStatus.ERROR
    assert result.code == "unsupported_tokens"
    assert result.intent is None


# --- Aufgabe 3: Alarm-Gate im Router ----------------------------------------
@pytest.mark.parametrize(
    "utterance",
    ["Wecker auf 7 Uhr", "Stell einen Wecker für morgen um 6", "Ähm, Wecker auf 7 Uhr"],
)
def test_an_obvious_alarm_command_gets_a_short_help_answer(utterance: str) -> None:
    assert is_alarm_command(utterance) is True
    assert alarm_command_hint(utterance) == HINT_COMMAND


@pytest.mark.parametrize("utterance", ["Welchen Wecker habe ich", "Welche Wecker habe ich noch"])
def test_a_question_about_own_alarms_gets_the_list_hint(utterance: str) -> None:
    assert is_alarm_command(utterance) is True
    assert alarm_command_hint(utterance) == HINT_LIST


@pytest.mark.parametrize(
    "utterance",
    [
        "Wie funktioniert ein Wecker?",
        "Warum hat mein Wecker nicht geklingelt?",
        "Wie stoppe ich den Wecker morgen?",
        "Erzähl mir etwas über die Alpen",
    ],
)
def test_talking_about_alarms_stays_chat(utterance: str) -> None:
    """The gate is a shape check, not a search for the substring "weck"."""

    assert is_alarm_command(utterance) is False
    assert alarm_command_hint(utterance) is None


@pytest.mark.parametrize("value", [None, 42, b"Wecker auf 7 Uhr", ["Wecker"]])
def test_the_gate_survives_values_that_are_not_text(value: object) -> None:
    assert is_alarm_command(value) is False
    assert alarm_command_hint(value) is None
