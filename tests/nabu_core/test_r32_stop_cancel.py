"""R32: der Stopp-Nachweis darf nicht mit dem Assist-Turn sterben.

Befund R31 am echten Geraet: ``Nabu stop: enter`` wurde geloggt, ``Nabu stop:
exit confirmed=...`` nie - und es gab keine Fehlerzeile. Der Verdacht ist ein
abgebrochener Turn zwischen Fence und Bestaetigung. Dieser Test bricht den Turn
gezielt ab und verlangt den Nachweis trotzdem.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from nabu_core.alarm_engine import AlarmStore
from nabu_core.alarm_session import AlarmSession
from nabu_core.dispatch import DispatchResult
from nabu_core.scheduler import Scheduler


class SlowStopDispatcher:
    """Ringt, bis der Stopp kommt, und braucht danach noch einen Moment."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stop_called = asyncio.Event()

    async def ring(self, target: str) -> DispatchResult:
        self.started.set()
        await self.stop_called.wait()
        return DispatchResult("stopped")

    async def stop(self) -> bool:
        # Das ist das Fenster: die Bestaetigung des Stopps dauert, und genau hier
        # kann der Assist-Turn abgebrochen werden.
        await asyncio.sleep(0.3)
        self.stop_called.set()
        return True


async def test_stop_confirmation_and_log_survive_a_cancelled_turn(tmp_path, caplog):
    now = datetime(2026, 11, 25, 7, 0, tzinfo=UTC)
    store = AlarmStore(tmp_path / "alarms.sqlite")
    store.create(now, now=now - timedelta(seconds=1))
    dispatcher = SlowStopDispatcher()

    async def state() -> str:
        return "idle" if dispatcher.stop_called.is_set() else "playing"

    def factory(target: str) -> AlarmSession:
        return AlarmSession(
            ring=lambda: dispatcher.ring(target),
            stop=dispatcher.stop,
            state_of=state,
            poll=0.01,
            ring_seconds=10,
        )

    scheduler = Scheduler(store, dispatcher, "fake-player", session_factory=factory)
    ring_task = asyncio.create_task(scheduler.tick(now))
    await asyncio.wait_for(dispatcher.started.wait(), 1)

    with caplog.at_level(logging.WARNING):
        stop_task = asyncio.create_task(scheduler.stop(now))
        # Mitten in der Bestaetigung abbrechen, nicht danach.
        await asyncio.sleep(0.05)
        assert not stop_task.done(), "der Stopp muss noch laufen"
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        # Der entkoppelte Stopp bekommt seine Zeit, den Nachweis zu schreiben.
        await asyncio.sleep(0.6)

    messages = [record.getMessage() for record in caplog.records]
    assert any("Nabu stop: enter" in message for message in messages), messages
    assert any("Nabu stop: exit confirmed=" in message for message in messages), messages

    occurrences = scheduler.occurrences()
    assert [item.status for item in occurrences] == ["skipped"]

    await asyncio.wait_for(ring_task, 2)
