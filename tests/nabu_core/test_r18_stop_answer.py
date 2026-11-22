"""R18: Die Stopp-Antwort darf nicht mehr behaupten, als bewiesen ist.

R11.1 verlangt: "Ein beendeter Prozess ohne bekannten Rueckgabecode ist nicht
automatisch erfolgreich." Fuer den Stopp heisst das: Die gesprochene Antwort darf
"gestoppt" nur sagen, wenn die Session die Stille bestaetigt hat. Ohne Ring darf
gar kein Geraet angesprochen werden.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nabu_core.alarm_engine import AlarmStore
from nabu_core.alarm_session import AlarmSession
from nabu_core.api import AlarmService, VoiceAlarmAdapter
from nabu_core.dispatch import DispatchResult
from nabu_core.ha_services import AlarmServiceLayer
from nabu_core.scheduler import Scheduler

MOMENT = datetime(2026, 11, 22, 16, 25, tzinfo=UTC)


def build_adapter(tmp_path, *, ringing: bool, confirmation: bool):
    calls: list[str] = []

    async def stop_callback() -> bool:
        calls.append("stop")
        return confirmation

    service = AlarmService(AlarmStore(tmp_path / "alarms.sqlite"))
    voice = VoiceAlarmAdapter(
        service,
        stop_callback=stop_callback,
        ringing_callback=lambda: ringing,
    )
    return voice, calls


class FakeScheduler:
    def __init__(self, *, ringing: bool) -> None:
        self._ringing = ringing
        self.stops = 0

    def is_ringing(self) -> bool:
        return self._ringing

    async def stop(self, now: datetime) -> bool:
        self.stops += 1
        return True


def build_layer(tmp_path, *, ringing: bool, confirmation: bool):
    calls: list[str] = []
    scheduler = FakeScheduler(ringing=ringing)

    async def stop_callback() -> bool:
        calls.append("stop")
        return confirmation

    layer = AlarmServiceLayer(
        AlarmStore(tmp_path / "alarms.sqlite"),
        stop_callback=stop_callback,
        scheduler=scheduler,
    )
    return layer, calls, scheduler


@pytest.mark.asyncio
async def test_confirmed_stop_is_reported_as_stopped(tmp_path) -> None:
    voice, calls = build_adapter(tmp_path, ringing=True, confirmation=True)

    result = await voice.handle("Oh, Stopp.", now=MOMENT)

    assert result.code == "STOP_REQUESTED"
    assert result.confirmation == "Wecker gestoppt."
    assert calls == ["stop"]


@pytest.mark.asyncio
async def test_unconfirmed_stop_is_not_reported_as_silence(tmp_path) -> None:
    voice, _ = build_adapter(tmp_path, ringing=True, confirmation=False)

    result = await voice.handle("Stopp.", now=MOMENT)

    assert result.code == "STOP_REQUESTED"
    assert result.confirmation is not None
    assert "nicht bestätigt" in result.confirmation
    assert result.confirmation != "Wecker gestoppt."


@pytest.mark.asyncio
async def test_named_stop_without_a_ring_touches_no_device(tmp_path) -> None:
    layer, calls, scheduler = build_layer(tmp_path, ringing=False, confirmation=True)

    result = await layer.handle_voice("Wecker aus.", now=MOMENT)

    assert result.code == "NOT_RINGING"
    assert result.message == "Es klingelt gerade kein Wecker."
    assert calls == [], "ohne Ring darf kein Player angesprochen werden"
    assert scheduler.stops == 0


@pytest.mark.asyncio
async def test_spoken_stop_during_a_ring_is_confirmed_by_the_session(tmp_path) -> None:
    layer, calls, scheduler = build_layer(tmp_path, ringing=True, confirmation=True)

    result = await layer.handle_voice("Oh, Stopp.", now=MOMENT)

    assert result.code == "STOP_REQUESTED"
    assert result.message == "Wecker gestoppt."
    assert calls == ["stop"]
    # Der Stopp laeuft ueber den injizierten Callback, nicht direkt ueber den Scheduler.
    assert scheduler.stops == 0


class PulsingDispatcher:
    """Bildet die reale 2-s-Datei mit Pause nach (wie im R09.5-Test)."""

    def __init__(self, play_seconds: float = 0.4) -> None:
        self.play_seconds = play_seconds
        self.playing = False
        self.starts = 0
        self.first_start = asyncio.Event()

    async def ring(self, target):  # noqa: ANN001, ANN201 - test double
        self.starts += 1
        self.first_start.set()
        self.playing = True
        try:
            await asyncio.sleep(self.play_seconds)
        finally:
            self.playing = False
        return DispatchResult("completed")

    async def stop(self) -> bool:
        self.playing = False
        return True


@pytest.mark.asyncio
async def test_spoken_stop_ends_the_ring_and_nothing_restarts(tmp_path) -> None:
    """Nachweis: Dienstantwort bestaetigt, Journal vermerkt stop_confirmed, keine
    erneute Wiedergabe."""

    now = datetime(2026, 11, 22, 7, 0, tzinfo=UTC)
    store = AlarmStore(tmp_path / "alarms.sqlite")
    store.create(now, now=now - timedelta(seconds=1))
    dispatcher = PulsingDispatcher()

    async def state() -> str:
        return "playing" if dispatcher.playing else "idle"

    def factory(target):
        return AlarmSession(
            ring=lambda: dispatcher.ring(target),
            stop=dispatcher.stop,
            state_of=state,
            poll=0.02,
            ring_seconds=30.0,
            restart_delay=0.05,
            max_restarts=60,
        )

    scheduler = Scheduler(store, dispatcher, "fake-player", session_factory=factory)
    layer = AlarmServiceLayer(
        store,
        stop_callback=lambda: scheduler.stop(now),
        scheduler=scheduler,
    )

    tick = asyncio.create_task(scheduler.tick(now))
    await asyncio.wait_for(dispatcher.first_start.wait(), 2)
    await asyncio.sleep(0.6)  # mitten im Ring, mehrere Pulse

    result = await layer.handle_voice("Oh, Stopp.", now=now)
    await asyncio.wait_for(tick, 5)

    starts_after_stop = dispatcher.starts
    await asyncio.sleep(0.3)

    assert result.code == "STOP_REQUESTED"
    assert result.message == "Wecker gestoppt."
    assert scheduler.occurrences()[0].detail == "stop_confirmed"
    assert scheduler.occurrences()[0].status == "skipped"
    assert dispatcher.starts == starts_after_stop, "nach dem Stopp darf nichts neu starten"
    assert dispatcher.playing is False
