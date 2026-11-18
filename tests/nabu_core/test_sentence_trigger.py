"""The sentence-trigger template and the parser must not drift apart.

Home Assistant evaluates a sentence trigger *before* its local intents, which is
the only reliable way to route spoken alarms to Nabu Core while
``prefer_local_intents`` stays on. That only works if every sentence in the
template is also understood by the parser that the service runs. These tests
read the real template file and check exactly that.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from nabu_core.voice_alarm import AlarmParser, ParseStatus

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 3, 18, 12, 30, tzinfo=BERLIN)  # a Wednesday
TEMPLATE = Path(__file__).resolve().parents[2] / "docs" / "ha-sentence-trigger.yaml"

REPLACEMENTS = {"minutes": "2", "hour": "7", "name": "frühschicht"}


def load_sentences() -> list[str]:
    document = yaml.safe_load(TEMPLATE.read_text(encoding="utf8"))
    trigger = document["trigger"][0]
    return list(trigger["command"])


def fill(sentence: str) -> str:
    """Turn a HassIL template into the sentence a user would really say."""
    text = sentence
    for slot, value in REPLACEMENTS.items():
        text = text.replace("{" + slot + "}", value)
    return text


def test_the_template_exists_and_has_sentences():
    sentences = load_sentences()

    assert sentences
    assert len(sentences) >= 15


@pytest.mark.parametrize("sentence", load_sentences())
def test_every_trigger_sentence_is_understood_by_the_parser(sentence):
    """A trigger that fires on a sentence the parser rejects would answer nothing."""
    parser = AlarmParser(timezone=BERLIN)

    result = parser.parse(fill(sentence), now=NOW)

    assert result.status is ParseStatus.OK, (sentence, result)
    assert result.intent is not None


_TIMER_SHAPE = re.compile(r"\btimer\b|\bin \S+ (?:minuten?|stunden?)\b")


@pytest.mark.parametrize("sentence", load_sentences())
def test_no_trigger_sentence_steals_a_timer_command(sentence):
    """Timer commands must keep reaching Home Assistant's own timer intents.

    A relative duration alone is not a collision - "Wecker in 2 Minuten" names an
    alarm, not a timer. What must never happen is that a plain "Timer ..." phrase
    ends up in this trigger.
    """
    lowered = fill(sentence).casefold()

    assert "timer" not in lowered
    if _TIMER_SHAPE.search(lowered):
        # A bare duration could be either; it is only ours when it says "Wecker"
        # or asks to be woken.
        assert "wecker" in lowered or "weck" in lowered


def test_the_template_calls_the_free_text_service():
    document = yaml.safe_load(TEMPLATE.read_text(encoding="utf8"))

    text = yaml.safe_dump(document, allow_unicode=True)
    assert "nabu_core.alarm_utterance" in text
    assert "trigger.sentence" in text


def test_the_template_is_not_an_active_automation():
    """It lives in docs/ so Home Assistant never loads it by accident."""
    assert TEMPLATE.parent.name == "docs"
    assert not re.search(r"automations\.yaml$", str(TEMPLATE))


# --- variant 1: "Wecker" belongs here, "Timer" belongs to Home Assistant ----
ALARM_PHRASES = [
    "Wecker in 15 Minuten",
    "Weck mich morgen um acht",
    "Stell einen Wecker auf 7 Uhr",
    "Erstelle einen Wecker namens Pilot in 2 Minuten",
    "Wecker aus",
    "Nabu stopp",
    "Hör auf zu klingeln",
]

TIMER_PHRASES = [
    "Timer in 15 Minuten",
    "Starte einen Timer für 10 Minuten",
    "Stell einen Timer auf 5 Uhr",
    "Pizza-Timer für 12 Minuten",
    "Wie viel Zeit hat der Timer noch",
    "Stopp den Timer",
]


@pytest.mark.parametrize("utterance", ALARM_PHRASES)
def test_alarm_phrases_belong_to_nabu_core(utterance):
    parser = AlarmParser(timezone=BERLIN)

    result = parser.parse(utterance, now=NOW)

    assert result.status is ParseStatus.OK, (utterance, result)


@pytest.mark.parametrize("utterance", TIMER_PHRASES)
def test_timer_phrases_are_not_alarm_commands(utterance):
    """A timer sentence must stay with Home Assistant, never with Nabu Core."""
    parser = AlarmParser(timezone=BERLIN)

    result = parser.parse(utterance, now=NOW)

    assert result.status is ParseStatus.ERROR, (utterance, result)


@pytest.mark.parametrize("utterance", TIMER_PHRASES)
def test_no_template_line_matches_a_timer_phrase(utterance):
    """The trigger must not fire for any of these."""
    filled = fill(utterance)
    lowered = filled.casefold()

    assert "timer" in lowered
    for sentence in load_sentences():
        assert lowered != fill(sentence).casefold()
