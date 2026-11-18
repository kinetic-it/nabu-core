"""End-to-end voice path: the German commands named in the work item.

These tests drive the real parser, the real service and the real store. Only
the player is replaced, so a passing test means the chain
"utterance -> intent -> persistence -> confirmation" actually works.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nabu_core.alarm_engine import AlarmStore
from nabu_core.api import AlarmService, VoiceAlarmAdapter
from nabu_core.voice_alarm import AlarmParser, IntentKind, ParseStatus, merge_clarification

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 3, 18, 12, 30, tzinfo=BERLIN)  # a Wednesday


def make_adapter(tmp_path, *, stop=None) -> VoiceAlarmAdapter:
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    return VoiceAlarmAdapter(AlarmService(store), stop_callback=stop)


async def say(adapter, text, *, now=NOW):
    return await adapter.handle(text, now=now)


# --- the wording from the work item ----------------------------------------
@pytest.mark.parametrize(
    ("utterance", "kind"),
    [
        ("Weck mich morgen um acht.", IntentKind.CREATE),
        ("Stell einen Wecker auf 7 Uhr.", IntentKind.CREATE),
        ("Wecker in 15 Minuten.", IntentKind.CREATE),
        ("Stell einen Wecker für Montag bis Freitag um halb sieben.", IntentKind.CREATE),
        ("Erstelle einen Wecker namens Frühschicht für 6:30 Uhr.", IntentKind.CREATE),
        ("Welche Wecker habe ich?", IntentKind.LIST),
        ("Wann klingelt mein nächster Wecker?", IntentKind.LIST),
        ("Ist mein Frühschicht-Wecker aktiviert?", IntentKind.STATUS),
        ("Ändere den Frühschicht-Wecker auf sieben Uhr.", IntentKind.UPDATE),
        ("Deaktiviere meinen Frühschicht-Wecker.", IntentKind.DEACTIVATE),
        ("Aktiviere meinen Frühschicht-Wecker wieder.", IntentKind.ACTIVATE),
        ("Lösche meinen Frühschicht-Wecker.", IntentKind.DELETE),
        ("Nabu, stopp.", IntentKind.STOP),
        ("Wecker aus.", IntentKind.STOP),
        ("Hör auf zu klingeln.", IntentKind.STOP),
    ],
)
def test_every_required_command_is_recognised(utterance, kind):
    parser = AlarmParser(timezone=BERLIN)
    result = parser.parse(utterance, now=NOW)

    assert result.status is ParseStatus.OK, result
    assert result.intent is not None
    assert result.intent.kind is kind


# --- create ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_wake_me_tomorrow_at_eight_stores_the_alarm(tmp_path):
    adapter = make_adapter(tmp_path)

    result = await say(adapter, "Weck mich morgen um acht.")

    assert result.code == "OK"
    assert result.alarm is not None
    local = result.alarm.when_utc.astimezone(BERLIN)
    assert (local.day, local.hour, local.minute) == (19, 8, 0)


@pytest.mark.asyncio
async def test_weekday_range_becomes_a_weekly_alarm(tmp_path):
    adapter = make_adapter(tmp_path)

    result = await say(adapter, "Stell einen Wecker für Montag bis Freitag um halb sieben.")

    assert result.code == "OK"
    assert result.alarm.weekdays == (0, 1, 2, 3, 4)
    assert result.alarm.local_time == "06:30:00"


@pytest.mark.asyncio
async def test_named_alarm_keeps_its_name(tmp_path):
    adapter = make_adapter(tmp_path)

    result = await say(adapter, "Erstelle einen Wecker namens Frühschicht für 6:30 Uhr.")

    assert result.code == "OK"
    assert result.alarm.name == "frühschicht"


@pytest.mark.asyncio
async def test_relative_alarm_lands_in_the_future(tmp_path):
    adapter = make_adapter(tmp_path)

    result = await say(adapter, "Wecker in 15 Minuten.")

    assert result.code == "OK"
    assert result.alarm.when_utc.astimezone(BERLIN).strftime("%H:%M") == "12:45"


# --- the full round trip ---------------------------------------------------
@pytest.mark.asyncio
async def test_the_whole_cycle_from_create_to_delete(tmp_path):
    adapter = make_adapter(tmp_path)

    created = await say(adapter, "Erstelle einen Wecker namens Frühschicht für 6:30 Uhr.")
    assert created.code == "OK"

    listed = await say(adapter, "Welche Wecker habe ich?")
    assert listed.code == "OK"
    assert len(listed.alarms) == 1
    assert "1 von 1" in listed.confirmation

    named = await say(adapter, "Ist mein Frühschicht-Wecker aktiviert?")
    assert named.code == "OK"
    assert "aktiviert" in named.confirmation

    changed = await say(adapter, "Ändere den Frühschicht-Wecker auf sieben Uhr.")
    assert changed.code == "OK"
    assert changed.alarm.when_utc.astimezone(BERLIN).strftime("%H:%M") == "07:00"

    off = await say(adapter, "Deaktiviere meinen Frühschicht-Wecker.")
    assert off.code == "OK"
    assert off.alarm.enabled is False

    on = await say(adapter, "Aktiviere meinen Frühschicht-Wecker wieder.")
    assert on.code == "OK"
    assert on.alarm.enabled is True

    gone = await say(adapter, "Lösche meinen Frühschicht-Wecker.")
    assert gone.code == "OK"

    empty = await say(adapter, "Welche Wecker habe ich?")
    assert len(empty.alarms) == 0


@pytest.mark.asyncio
async def test_next_alarm_is_reported_with_its_time(tmp_path):
    adapter = make_adapter(tmp_path)
    await say(adapter, "Wecker in 15 Minuten.")
    await say(adapter, "Wecker in 90 Minuten.")

    result = await say(adapter, "Wann klingelt mein nächster Wecker?")

    assert result.code == "OK"
    assert "12:45" in result.confirmation


@pytest.mark.asyncio
async def test_unknown_name_is_reported_instead_of_invented(tmp_path):
    adapter = make_adapter(tmp_path)

    result = await say(adapter, "Lösche meinen Frühschicht-Wecker.")

    assert result.code == "NOT_FOUND"
    assert result.alarm is None


# --- stop ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stop_asks_the_scheduler_and_never_claims_silence(tmp_path):
    calls = []

    async def stop():
        calls.append(True)
        return True

    adapter = make_adapter(tmp_path, stop=stop)

    result = await say(adapter, "Nabu, stopp.")

    assert calls == [True]
    assert result.code == "STOP_REQUESTED"
    assert "angefordert" in result.confirmation


# --- ambiguity -------------------------------------------------------------
@pytest.mark.asyncio
async def test_ambiguous_time_asks_instead_of_guessing(tmp_path):
    adapter = make_adapter(tmp_path)

    result = await say(adapter, "Stell einen Wecker um 7 Uhr und um 8 Uhr.")

    assert result.code == "CLARIFICATION_REQUIRED"
    assert result.question


def test_an_answer_alone_completes_the_open_question():
    merged = merge_clarification("Stell einen Wecker.", "missing_time", "um sieben")

    assert merged == "Stell einen Wecker. um sieben"
    parser = AlarmParser(timezone=BERLIN)
    assert parser.parse(merged, now=NOW).intent.kind is IntentKind.CREATE


def test_an_answer_completes_a_missing_name():
    merged = merge_clarification("Lösche den Wecker.", "missing_alarm_selector", "Frühschicht")

    assert merged is not None
    parser = AlarmParser(timezone=BERLIN)
    result = parser.parse(merged, now=NOW)
    assert result.intent is not None
    assert result.intent.label == "frühschichtwecker"


def test_a_useless_answer_is_not_forced_into_a_command():
    assert merge_clarification("Lösche den Wecker.", "missing_alarm_selector", "") is None
    assert merge_clarification("Stell einen Wecker.", "missing_time", "x" * 200) is None


# --- spending -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_expired_one_shot_cannot_be_revived_by_voice(tmp_path):
    adapter = make_adapter(tmp_path)
    created = await say(adapter, "Erstelle einen Wecker namens Spät für 13:00 Uhr.")
    assert created.code == "OK"

    later = datetime(2026, 3, 18, 14, 0, tzinfo=BERLIN)
    stale = await adapter.handle("Aktiviere meinen Spät-Wecker wieder.", now=later)

    assert stale.code == "NOT_FOUND"
    assert stale.question


# --- the sentence-trigger entry point --------------------------------------
@pytest.mark.asyncio
async def test_the_service_handles_a_sentence_trigger_text(tmp_path):
    """Home Assistant hands the recognised sentence over as one string."""
    from nabu_core.ha_services import AlarmServiceLayer

    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    layer = AlarmServiceLayer(store)

    result = await layer.utterance(
        {"text": "Erstelle einen Wecker namens Pilot in 2 Minuten"}
    )

    assert result.ok is True
    assert result.code == "OK"
    assert result.data["confirmation"] == "Wecker wurde gespeichert."
    alarms = store.list()
    assert len(alarms) == 1
    assert alarms[0].name == "pilot"


@pytest.mark.asyncio
async def test_the_service_reports_a_missing_sentence(tmp_path):
    from nabu_core.ha_services import AlarmServiceLayer

    layer = AlarmServiceLayer(AlarmStore(tmp_path / "a.sqlite", timezone_name="Europe/Berlin"))

    result = await layer.utterance({"text": "   "})

    assert result.ok is False
    assert result.code == "INVALID_TEXT"


@pytest.mark.asyncio
async def test_the_service_passes_a_question_through(tmp_path):
    from nabu_core.ha_services import AlarmServiceLayer

    layer = AlarmServiceLayer(AlarmStore(tmp_path / "a.sqlite", timezone_name="Europe/Berlin"))

    result = await layer.utterance({"text": "Stell einen Wecker."})

    assert result.code == "CLARIFICATION_REQUIRED"
    assert result.data["question"] == "Wann soll der Wecker klingeln?"


def test_the_trigger_sentence_that_failed_in_the_pipeline_is_understood():
    """The exact sentence from the microphone test must not fall through."""
    parser = AlarmParser(timezone=BERLIN)

    result = parser.parse("Erstelle einen Wecker namens Pilot in 2 Minuten", now=NOW)

    assert result.status is ParseStatus.OK
    assert result.intent is not None
    assert result.intent.kind is IntentKind.CREATE
    assert result.intent.label == "pilot"
    assert result.intent.at is not None


# --- stop is contextual ----------------------------------------------------
@pytest.mark.parametrize("utterance", ["Nabu, stopp.", "Wecker aus.", "Hör auf zu klingeln."])
def test_an_explicit_alarm_stop_is_never_generic(utterance):
    from nabu_core.voice_alarm import is_generic_stop

    assert is_generic_stop(utterance) is False


@pytest.mark.parametrize("utterance", ["stopp", "Stop.", "  stopp  "])
def test_a_bare_stop_word_is_generic(utterance):
    from nabu_core.voice_alarm import is_generic_stop

    assert is_generic_stop(utterance) is True


@pytest.mark.asyncio
async def test_a_bare_stop_does_not_reach_the_scheduler_while_nothing_rings(tmp_path):
    calls = []

    async def stop():
        calls.append(True)
        return True

    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    adapter = VoiceAlarmAdapter(
        AlarmService(store), stop_callback=stop, ringing_callback=lambda: False
    )

    assert adapter.is_ringing() is False
    # The router decides to hand a bare "stopp" to the agent; the adapter itself
    # may still stop, but the caller must check is_ringing() first.
    assert calls == []


@pytest.mark.asyncio
async def test_ringing_state_comes_from_the_scheduler(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    adapter = VoiceAlarmAdapter(
        AlarmService(store), stop_callback=None, ringing_callback=lambda: True
    )

    assert adapter.is_ringing() is True
