"""Snooze voice command and honest stop error propagation."""

from datetime import UTC, datetime

from nabu_core.alarm_engine import AlarmStore
from nabu_core.api import AlarmService, VoiceAlarmAdapter
from nabu_core.ha_services import AlarmServiceLayer


async def test_voice_snooze_invokes_exact_duration(tmp_path):
    store = AlarmStore(tmp_path / "alarm.sqlite")
    calls = []

    async def snooze(minutes):
        calls.append(minutes)
        return True

    voice = VoiceAlarmAdapter(AlarmService(store), snooze_callback=snooze)
    result = await voice.handle("Noch fünf Minuten", now=datetime.now(UTC))
    assert result.code == "SNOOZE_REQUESTED"
    assert calls == [5]


async def test_stop_failure_is_not_acknowledged_as_requested(tmp_path):
    store = AlarmStore(tmp_path / "alarm.sqlite")

    async def fail():
        raise RuntimeError("db unavailable")

    layer = AlarmServiceLayer(store, stop_callback=fail)
    result = await layer.voice.handle("Stopp den Wecker", now=datetime.now(UTC))
    assert result.code == "STOP_FAILED"
