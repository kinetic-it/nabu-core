"""End-to-end voice routing: German utterance -> parser -> engine -> speaker.

Covers the agreed natural phrasings including written-out numbers, weekday
repetition and named alarms. Nothing here asserts audio output; the engine
answer is the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nabu_core.alarm_engine import AlarmStore
from nabu_core.ha_services import AlarmServiceLayer

MOMENT = datetime(2026, 3, 18, 7, 30, tzinfo=UTC)  # Wednesday


def build(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    return AlarmServiceLayer(store, stop_callback=_stop)


async def _stop() -> bool:
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("Stell einen Wecker in zehn Minuten", {"kind": "once"}),
        ("Wecker in 15 Minuten", {"kind": "once"}),
        ("Stell jeden Montag um 8 Uhr einen Wecker", {"weekdays": [0]}),
        (
            "Stell jeden Dienstag und Donnerstag um halb sieben einen Wecker",
            {"weekdays": [1, 3]},
        ),
        ("Stell Sonntag um 12 Uhr einen Medikamentenwecker", {"name": "medikamentenwecker"}),
        ("Stell mir morgen um sieben Uhr einen Wecker", {"kind": "once"}),
    ],
)
async def test_natural_phrasings_create_alarms(tmp_path, utterance, expected):
    service = build(tmp_path)

    result = await service.handle_voice(utterance, now=MOMENT)

    assert result.code == "OK", utterance
    alarm = result.data["alarm"]
    for key, value in expected.items():
        assert alarm[key] == value, (utterance, key)


@pytest.mark.asyncio
async def test_list_delete_and_stop_phrasings(tmp_path):
    service = build(tmp_path)
    await service.handle_voice("Stell einen Wecker in zehn Minuten", now=MOMENT)
    await service.handle_voice("Stell Sonntag um 12 Uhr einen Zahnarztwecker", now=MOMENT)

    listed = await service.handle_voice("Welche Wecker habe ich?", now=MOMENT)
    assert listed.data["count"] == 2

    deleted = await service.handle_voice("Lösche den Zahnarztwecker", now=MOMENT)
    assert deleted.code == "OK"
    assert service.list().data["count"] == 1

    stopped = await service.handle_voice("Stopp den Wecker", now=MOMENT)
    assert stopped.code == "STOP_REQUESTED"


@pytest.mark.asyncio
async def test_unsupported_or_ambiguous_phrasings_stay_silent(tmp_path):
    service = build(tmp_path)

    for utterance in (
        "Stell einen Wecker",
        "Schalte das Licht ein",
        "Wecker um 25 Uhr",
    ):
        result = await service.handle_voice(utterance, now=MOMENT)
        assert result.ok is False, utterance
    assert service.list().data["count"] == 0
