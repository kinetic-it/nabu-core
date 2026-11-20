"""Integration: one logical occurrence can supervise multiple media attempts."""

import asyncio
from datetime import UTC, datetime, timedelta

from nabu_core.alarm_engine import AlarmStore
from nabu_core.alarm_session import AlarmSession
from nabu_core.dispatch import DispatchResult
from nabu_core.scheduler import Scheduler


async def test_scheduler_preserves_occurrence_until_logical_stop(tmp_path):
    now = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)
    store = AlarmStore(tmp_path / "alarms.sqlite")
    store.create(now, now=now - timedelta(seconds=1))

    class FakeDispatcher:
        def __init__(self):
            self.calls = 0
            self.restarted = asyncio.Event()
            self.done = asyncio.Event()

        async def ring(self, target):
            self.calls += 1
            if self.calls == 1:
                return DispatchResult("completed")
            self.restarted.set()
            await self.done.wait()
            return DispatchResult("stopped")

        async def stop(self):
            self.done.set()
            return True

    dispatcher = FakeDispatcher()

    async def state():
        return "playing" if not dispatcher.done.is_set() else "idle"

    def session_factory(target):
        return AlarmSession(
            ring=lambda: dispatcher.ring(target),
            stop=dispatcher.stop,
            state_of=state,
            poll=0.01,
            ring_seconds=10,
        )

    scheduler = Scheduler(store, dispatcher, "fake-player", session_factory=session_factory)
    task = asyncio.create_task(scheduler.tick(now))
    await asyncio.wait_for(dispatcher.restarted.wait(), 0.5)
    assert [item.status for item in scheduler.occurrences()] == ["in_flight"]
    assert scheduler.is_ringing()
    await scheduler.stop(now)
    await asyncio.wait_for(task, 1)
    assert dispatcher.calls == 2
    assert scheduler.occurrences()[0].status == "skipped"


async def test_other_scheduler_stop_fences_running_session(tmp_path):
    now = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)
    store = AlarmStore(tmp_path / "alarms.sqlite")
    store.create(now, now=now - timedelta(seconds=1))

    class Dispatcher:
        def __init__(self):
            self.started = asyncio.Event()
            self.done = asyncio.Event()

        async def ring(self, target):
            self.started.set()
            await self.done.wait()
            return DispatchResult("stopped")

        async def stop(self):
            self.done.set()
            return True

    first, second = Dispatcher(), Dispatcher()

    async def state():
        return "idle" if first.done.is_set() else "playing"

    def factory(target):
        return AlarmSession(
            ring=lambda: first.ring(target),
            stop=first.stop,
            state_of=state,
            poll=0.01,
            ring_seconds=10,
        )

    scheduler = Scheduler(store, first, "fake-player", session_factory=factory)
    other = Scheduler(store, second, "fake-player")
    task = asyncio.create_task(scheduler.tick(now))
    await asyncio.wait_for(first.started.wait(), 0.5)
    await other.stop(now)
    await asyncio.wait_for(task, 1)
    assert first.done.is_set()
    assert scheduler.occurrences()[0].status == "skipped"
