from datetime import UTC, datetime, timedelta

import pytest

from nabu_core.alarm_engine import AlarmStore
from nabu_core.api import AlarmService, VoiceAlarmAdapter
from nabu_core.dispatch import SerialDispatcher
from nabu_core.scheduler import Scheduler


class FakePlayer:
    def __init__(self):
        self.calls = []

    async def play(self, target):
        self.calls.append(target)

    async def stop(self, target):
        return False


@pytest.mark.asyncio
async def test_voice_through_commit_due_play_and_restart(tmp_path):
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    path = tmp_path / "alarms.sqlite"
    store = AlarmStore(path, timezone_name="Europe/Berlin")
    fake = FakePlayer()
    channel = SerialDispatcher(fake, allowed_targets={"mock"}, max_seconds=1)
    scheduler = Scheduler(store, channel, "mock")
    voice = VoiceAlarmAdapter(AlarmService(store), stop_callback=lambda: scheduler.stop(now))
    created = await voice.handle("Stell einen Wecker in zehn Minuten", now=now)
    assert created.code == "OK"
    assert created.alarm.when_utc == now + timedelta(minutes=10)
    assert len(AlarmStore(path, timezone_name="Europe/Berlin").list()) == 1
    assert await scheduler.tick(now + timedelta(minutes=9)) == []
    result = await scheduler.tick(now + timedelta(minutes=10))
    assert [item.status for item in result] == ["completed"]
    assert fake.calls == ["mock"]
    restarted = Scheduler(AlarmStore(path, timezone_name="Europe/Berlin"), channel, "mock")
    assert restarted.recover_interrupted() == 0
    assert await restarted.tick(now + timedelta(minutes=10, seconds=1)) == []
    assert fake.calls == ["mock"]
