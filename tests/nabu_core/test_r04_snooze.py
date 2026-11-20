"""Snooze is durable and must not undo a subsequent stop."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nabu_core.alarm_engine import AlarmStore
from nabu_core.alarm_session import AlarmSession
from nabu_core.dispatch import DispatchResult
from nabu_core.scheduler import Scheduler


@pytest.mark.parametrize("stop_while_snoozed", [False, True])
async def test_snooze_is_durable_then_resumes(tmp_path, stop_while_snoozed):
    now = datetime.now(UTC)
    store = AlarmStore(tmp_path / "alarm.sqlite")
    store.create(now, now=now - timedelta(seconds=1))

    class Dispatcher:
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.done = asyncio.Event()

        async def ring(self, target):
            self.calls += 1
            self.started.set()
            await self.done.wait()
            return DispatchResult("stopped")

        async def stop(self):
            self.done.set()
            return True

    dispatcher = Dispatcher()

    async def state():
        return "idle" if dispatcher.done.is_set() else "playing"

    def factory(target):
        dispatcher.done.clear()
        return AlarmSession(
            ring=lambda: dispatcher.ring(target),
            stop=dispatcher.stop,
            state_of=state,
            poll=0.01,
        )

    async def safe(target):
        return True

    scheduler = Scheduler(
        store, dispatcher, "media_player.lab", session_factory=factory, recovery_check=safe
    )
    first = asyncio.create_task(scheduler.tick(now))
    await asyncio.wait_for(dispatcher.started.wait(), 0.5)
    assert await scheduler.snooze(now, timedelta(minutes=5)) is True
    await asyncio.wait_for(first, 1)
    phase = store._run_read(lambda db: db.execute("SELECT phase FROM alarm_sessions").fetchone()[0])
    assert phase == "snoozed"
    assert scheduler.occurrences()[0].status == "in_flight"
    if stop_while_snoozed:
        await scheduler.stop(now + timedelta(minutes=1))
        await scheduler.tick(now + timedelta(minutes=5))
        assert dispatcher.calls == 1
        assert scheduler.occurrences()[0].status == "skipped"
        return
    await scheduler.tick(now + timedelta(minutes=4))
    assert dispatcher.calls == 1
    dispatcher.started.clear()
    second = asyncio.create_task(scheduler.tick(now + timedelta(minutes=5)))
    await asyncio.wait_for(dispatcher.started.wait(), 0.5)
    assert dispatcher.calls == 2
    await scheduler.stop(now + timedelta(minutes=5))
    await asyncio.wait_for(second, 1)
    assert scheduler.occurrences()[0].status == "skipped"
