"""R09.5: Ein Stopp waehrend eines laufenden Rings muss die Session beenden.

Hardware-Befund 2026-11-20: Bei laufendem Ring (2-s-Datei, 2,2-s-Pause)
meldete ``alarm_stop`` ``STOP_REQUESTED``/``confirmed: false``, der Alarm
pulsierte jedoch bis zur Deadline weiter (Log: ``restarting (n/60)``), und die
Occurrence endete als ``rang_out``. Der Stopp war also wirkungslos.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from nabu_core.alarm_engine import AlarmStore
from nabu_core.alarm_session import AlarmSession
from nabu_core.dispatch import DispatchResult
from nabu_core.scheduler import Scheduler


class PulsingDispatcher:
    """Bildet die reale 2-s-Datei mit 2-s-Pause nach."""

    def __init__(self, play_seconds: float = 0.4):
        self.play_seconds = play_seconds
        self.playing = False
        self.starts = 0
        self.stops = 0
        self.first_start = asyncio.Event()

    async def ring(self, target):
        self.starts += 1
        self.first_start.set()
        self.playing = True
        try:
            await asyncio.sleep(self.play_seconds)
        finally:
            self.playing = False
        return DispatchResult("completed")

    async def stop(self):
        self.stops += 1
        self.playing = False
        return True


def _build(tmp_path, play_seconds=0.4, ring_seconds=30.0):
    now = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)
    store = AlarmStore(tmp_path / "alarms.sqlite")
    store.create(now, now=now - timedelta(seconds=1))
    dispatcher = PulsingDispatcher(play_seconds)

    async def state():
        return "playing" if dispatcher.playing else "idle"

    def factory(target):
        return AlarmSession(
            ring=lambda: dispatcher.ring(target),
            stop=dispatcher.stop,
            state_of=state,
            poll=0.02,
            ring_seconds=ring_seconds,
            restart_delay=0.05,
            max_restarts=60,
        )

    scheduler = Scheduler(store, dispatcher, "fake-player", session_factory=factory)
    return now, scheduler, dispatcher


async def test_stop_during_active_ring_ends_session(tmp_path):
    now, scheduler, dispatcher = _build(tmp_path)
    tick = asyncio.create_task(scheduler.tick(now))
    await asyncio.wait_for(dispatcher.first_start.wait(), 2)
    await asyncio.sleep(0.6)  # mehrere Pulse, mitten im Ring

    confirmed = await asyncio.wait_for(scheduler.stop(now), 20)
    await asyncio.wait_for(tick, 5)

    assert confirmed is True, "Stopp muss den laufenden Ring bestaetigen"
    assert scheduler.occurrences()[0].status == "skipped"
    assert scheduler.occurrences()[0].detail == "stop_confirmed"
    assert dispatcher.starts <= 6, f"Ring lief nach dem Stopp weiter ({dispatcher.starts} Starts)"


async def test_stop_arriving_between_pulses_ends_session(tmp_path):
    """Der Stopp darf nicht davon abhaengen, in welche Pulsphase er faellt."""
    now, scheduler, dispatcher = _build(tmp_path)
    tick = asyncio.create_task(scheduler.tick(now))
    await asyncio.wait_for(dispatcher.first_start.wait(), 2)
    await asyncio.sleep(0.15)  # wahrscheinlich in eine Pause

    confirmed = await asyncio.wait_for(scheduler.stop(now), 20)
    await asyncio.wait_for(tick, 5)

    assert confirmed is True
    assert scheduler.occurrences()[0].detail == "stop_confirmed"
