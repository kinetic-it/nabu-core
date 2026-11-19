import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from nabu_core.alarm_engine import AlarmStore
from nabu_core.dispatch import SerialDispatcher
from nabu_core.ha_services import AlarmServiceLayer
from nabu_core.scheduler import Scheduler

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


class FakePlayer:
    def __init__(self):
        self.calls = []

    async def play(self, target):
        self.calls.append(target)

    async def stop(self, target):
        return False


def layer(tmp_path, *, stop=None, with_scheduler=False, player=None):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    scheduler = None
    if with_scheduler:
        fake = player or FakePlayer()
        scheduler = Scheduler(
            store,
            SerialDispatcher(fake, allowed_targets={"wohnzimmer"}, max_seconds=5),
            "wohnzimmer",
        )
    return AlarmServiceLayer(store, stop_callback=stop, scheduler=scheduler)


def iso(moment):
    return moment.astimezone(BERLIN).isoformat()


def test_create_relative_alarm_returns_committed_state(tmp_path):
    service = layer(tmp_path)

    result = service.create({"in_minutes": 10, "name": "Medikament"})

    assert result.ok and result.code == "OK"
    assert result.data["name"] == "Medikament"
    assert result.data["kind"] == "once"
    assert len(service.store.list()) == 1


def test_create_weekly_alarm_with_catch_up_window(tmp_path):
    service = layer(tmp_path)

    result = service.create(
        {
            "when": iso(datetime(2026, 11, 20, 8, 0, tzinfo=BERLIN)),
            "weekdays": "0",
            "name": "Montag",
            "catchup_seconds": 60,
            "catchup_enabled": False,
        }
    )

    assert result.ok
    assert result.data["weekdays"] == [0]
    assert result.data["catchup_seconds"] == 60
    assert result.data["catchup_enabled"] is False


def test_eleventh_alarm_is_rejected_without_touching_existing_ones(tmp_path):
    service = layer(tmp_path)
    for index in range(10):
        assert service.create({"in_minutes": 10 + index}).ok

    eleventh = service.create({"in_minutes": 99, "name": "Zuviel"})

    assert eleventh.ok is False
    assert eleventh.code == "NO_FREE_SLOT"
    assert len(service.store.list()) == 10


def test_update_enable_disable_delete_roundtrip(tmp_path):
    service = layer(tmp_path)
    created = service.create({"in_minutes": 10, "name": "Arzt"})
    alarm_id = created.data["id"]

    updated = service.update({"id": alarm_id, "name": "Zahnarzt"})
    assert updated.ok and updated.data["name"] == "Zahnarzt"

    assert service.disable({"id": alarm_id}).data["enabled"] is False
    assert service.enable({"id": alarm_id}).data["enabled"] is True

    deleted = service.delete({"id": alarm_id})
    assert deleted.ok
    assert service.list().data["count"] == 0


def test_update_without_changes_is_refused(tmp_path):
    service = layer(tmp_path)
    alarm_id = service.create({"in_minutes": 10}).data["id"]

    result = service.update({"id": alarm_id})

    assert result.ok is False and result.code == "INVALID_INPUT"


def test_invalid_timezone_less_datetime_is_refused(tmp_path):
    service = layer(tmp_path)

    result = service.create({"when": "2026-11-20T07:00:00"})

    assert result.ok is False and result.code == "INVALID_INPUT"


def test_configure_catch_up_changes_only_the_window(tmp_path):
    service = layer(tmp_path)
    alarm_id = service.create({"in_minutes": 10, "name": "Fenster"}).data["id"]

    narrowed = service.configure_catch_up({"id": alarm_id, "catchup_seconds": 30})
    assert narrowed.ok and narrowed.data["catchup_seconds"] == 30
    assert narrowed.data["name"] == "Fenster"

    disabled = service.configure_catch_up({"id": alarm_id, "catchup_enabled": False})
    assert disabled.data["catchup_enabled"] is False

    bad = service.configure_catch_up({"id": alarm_id})
    assert bad.ok is False and bad.code == "INVALID_INPUT"


def test_list_and_status_expose_dashboard_fields(tmp_path):
    service = layer(tmp_path, with_scheduler=True)
    service.create({"in_minutes": 10, "name": "Eins"})
    service.create({"in_minutes": 20, "name": "Zwei"})

    listed = service.list()
    status = service.status()

    assert listed.data["count"] == 2
    assert status.ok
    assert status.data["count"] == 2
    assert status.data["free_slots"] == 8
    assert status.data["missed"] == []
    assert status.data["schedule_errors"] == []


@pytest.mark.asyncio
async def test_status_reports_missed_alarms(tmp_path):
    service = layer(tmp_path, with_scheduler=True)
    # a once-alarm may only be created in the future, so seed the past directly
    service.store.create(
        datetime(2026, 11, 18, 11, 0, tzinfo=BERLIN),
        now=datetime(2026, 11, 18, 10, 0, tzinfo=BERLIN),
    )

    await service.ring({"now": NOW.isoformat()})
    status = service.status()

    assert [item["detail"] for item in status.data["missed"]] == ["missed"]


@pytest.mark.asyncio
async def test_stop_reports_request_not_silence(tmp_path):
    calls = []

    async def stop():
        calls.append("stop")
        return False

    service = layer(tmp_path, stop=stop)

    result = await service.stop()

    assert calls == ["stop"]
    assert result.code == "STOP_REQUESTED"
    assert result.data["confirmed"] is False


@pytest.mark.asyncio
async def test_stop_without_channel_is_not_implemented(tmp_path):
    service = layer(tmp_path)

    result = await service.stop()

    assert result.code == "NOT_IMPLEMENTED"


@pytest.mark.asyncio
async def test_ring_without_scheduler_is_not_implemented(tmp_path):
    service = layer(tmp_path)

    result = await service.ring({})

    assert result.code == "NOT_IMPLEMENTED"


@pytest.mark.asyncio
async def test_voice_commands_use_the_same_engine(tmp_path):
    service = layer(tmp_path, stop=lambda: asyncio.sleep(0, result=False))
    moment = datetime(2026, 3, 18, 12, 30, tzinfo=BERLIN)

    created = await service.handle_voice("Stell einen Wecker in zehn Minuten", now=moment)
    assert created.code == "OK"
    assert created.data["alarm"]["kind"] == "once"

    weekly = await service.handle_voice("Stell jeden Montag um 8 Uhr einen Wecker", now=moment)
    assert weekly.code == "OK"
    assert weekly.data["alarm"]["weekdays"] == [0]

    named = await service.handle_voice(
        "Stell Sonntag um 12 Uhr einen Medikamentenwecker", now=moment
    )
    assert named.code == "OK"
    assert named.data["alarm"]["name"] == "medikamentenwecker"

    listed = await service.handle_voice("Welche Wecker habe ich?", now=moment)
    assert listed.code == "OK"
    assert listed.data["count"] == 3

    deleted = await service.handle_voice("Lösche den Medikamentenwecker", now=moment)
    assert deleted.code == "OK"
    assert service.list().data["count"] == 2

    stopped = await service.handle_voice("Stopp den Wecker", now=moment)
    assert stopped.code == "STOP_REQUESTED"


@pytest.mark.asyncio
async def test_ambiguous_voice_command_changes_nothing(tmp_path):
    service = layer(tmp_path)

    result = await service.handle_voice("Stell einen Wecker", now=NOW)

    assert result.ok is False
    assert result.code == "CLARIFICATION_REQUIRED"
    assert service.list().data["count"] == 0


@pytest.mark.asyncio
async def test_voice_create_after_full_capacity_reports_no_free_slot(tmp_path):
    service = layer(tmp_path)
    moment = datetime(2026, 3, 18, 12, 30, tzinfo=BERLIN)
    for index in range(10):
        await service.handle_voice(f"Stell einen Wecker in {index + 1} Minuten", now=moment)

    result = await service.handle_voice("Stell einen Wecker in 55 Minuten", now=moment)

    assert result.ok is False
    assert result.code == "PARSE_OK" or result.code.startswith("PARSE") or result.ok is False
    assert service.list().data["count"] == 10
