"""Durable session journal must survive a new store instance."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

from nabu_core.alarm_engine import AlarmStore
from nabu_core.alarm_session import AlarmSession
from nabu_core.dispatch import DispatchResult
from nabu_core.scheduler import Scheduler


def test_v2_migration_preserves_alarm_and_epoch(tmp_path):
    path = tmp_path / "alarm.sqlite"
    store = AlarmStore(path)
    now = datetime(2026, 11, 20, 7, tzinfo=UTC)
    original = store.create(now + timedelta(minutes=1), now=now)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE alarm_sessions")
        db.execute("PRAGMA user_version=2")
        db.execute("UPDATE alarm_metadata SET value='7' WHERE key='scheduler_stop_epoch'")
    migrated = AlarmStore(path)
    assert migrated.get(original.id).instance_id == original.instance_id
    assert migrated._run_read(lambda db: db.execute("PRAGMA user_version").fetchone()[0]) == 3
    assert (
        migrated._run_read(
            lambda db: db.execute(
                "SELECT value FROM alarm_metadata WHERE key='scheduler_stop_epoch'"
            ).fetchone()[0]
        )
        == "7"
    )


async def test_session_claim_persists_generation_target_and_stop_epoch(tmp_path):
    now = datetime(2026, 11, 20, 7, tzinfo=UTC)
    store = AlarmStore(tmp_path / "alarm.sqlite")
    store.create(now, now=now - timedelta(seconds=1))
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

    scheduler = Scheduler(store, dispatcher, "media_player.lab", session_factory=factory)
    task = asyncio.create_task(scheduler.tick(now))
    await asyncio.wait_for(started.wait(), 0.5)
    reloaded = AlarmStore(store.path)
    row = reloaded._run_read(lambda db: db.execute("SELECT * FROM alarm_sessions").fetchone())
    assert row["phase"] == "active"
    assert row["target"] == "media_player.lab"
    assert row["generation"]
    assert row["stop_epoch"] == 0
    await scheduler.stop(now)
    await asyncio.wait_for(task, 1)
    row = reloaded._run_read(lambda db: db.execute("SELECT * FROM alarm_sessions").fetchone())
    assert row["phase"] == "stopped"
