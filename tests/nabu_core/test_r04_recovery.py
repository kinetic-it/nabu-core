"""Restart recovery requires a durable generation and a safe target."""

import asyncio
from datetime import UTC, datetime, timedelta

from nabu_core.alarm_engine import AlarmStore
from nabu_core.alarm_session import AlarmSession
from nabu_core.dispatch import DispatchResult
from nabu_core.scheduler import Scheduler


async def test_restart_resumes_only_after_safe_target_check(tmp_path):
    now = datetime(2026, 11, 20, 7, tzinfo=UTC)
    store = AlarmStore(tmp_path / "alarms.sqlite")
    alarm = store.create(now, now=now - timedelta(seconds=1))
    started, done = asyncio.Event(), asyncio.Event()

    class Dispatcher:
        async def ring(self, target):
            started.set()
            await done.wait()
            return DispatchResult("stopped")

        async def stop(self):
            done.set()
            return True

    dispatcher = Dispatcher()

    async def state():
        return "idle" if done.is_set() else "playing"

    def factory(target):
        return AlarmSession(
            ring=lambda: dispatcher.ring(target),
            stop=dispatcher.stop,
            state_of=state,
            poll=0.01,
        )

    old = Scheduler(store, dispatcher, "media_player.lab", session_factory=factory)
    claim = old._claim(alarm, now, old._current_epoch())
    assert claim is not None
    old_generation = store._run_read(
        lambda db: db.execute("SELECT generation FROM alarm_sessions").fetchone()[0]
    )

    async def check(target):
        return target == "media_player.lab"

    new = Scheduler(
        store, dispatcher, "media_player.lab", session_factory=factory, recovery_check=check
    )
    task = asyncio.create_task(new.tick(now))
    await asyncio.wait_for(started.wait(), 0.5)
    generation = store._run_read(
        lambda db: db.execute("SELECT generation FROM alarm_sessions").fetchone()[0]
    )
    assert generation != old_generation
    await new.stop(now)
    await asyncio.wait_for(task, 1)
    assert new.occurrences()[0].detail == "stop_confirmed"


async def test_restart_does_not_override_foreign_media(tmp_path):
    now = datetime(2026, 11, 20, 7, tzinfo=UTC)
    store = AlarmStore(tmp_path / "alarms.sqlite")
    alarm = store.create(now, now=now - timedelta(seconds=1))
    calls = []

    class Dispatcher:
        async def ring(self, target):
            calls.append(target)
            return DispatchResult("completed")

    dispatcher = Dispatcher()

    def factory(target):
        raise AssertionError("unsafe recovery must never start")

    old = Scheduler(store, dispatcher, "media_player.lab", session_factory=factory)
    assert old._claim(alarm, now, old._current_epoch()) is not None

    async def check(target):
        return False

    new = Scheduler(
        store, dispatcher, "media_player.lab", session_factory=factory, recovery_check=check
    )
    await new.tick(now)
    assert calls == []
    assert new.occurrences()[0].status == "in_flight"
    await new.tick(now + timedelta(days=1))
    assert new.occurrences()[0].status == "interrupted"
