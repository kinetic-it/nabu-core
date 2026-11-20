import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nabu_core.alarm_engine import AlarmStore, ValidationError
from nabu_core.dispatch import SerialDispatcher
from nabu_core.scheduler import Scheduler

NOW = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)


class FakePlayer:
    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay

    async def play(self, target):
        self.calls.append(target)
        if self.delay:
            await asyncio.sleep(self.delay)

    async def stop(self, target):
        return False


def channel(player):
    return SerialDispatcher(player, allowed_targets={"wohnzimmer"}, max_seconds=5)


def make_scheduler(store, player, **kwargs):
    return Scheduler(store, channel(player), "wohnzimmer", **kwargs)


# ---------------------------------------------------------------- Schema v1


def write_v1_database(path: Path) -> None:
    """Create the exact pre-catch-up schema that existed before this task."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE alarm_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE alarms (
            id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL UNIQUE,
            active_since TEXT NOT NULL,
            name TEXT,
            name_key TEXT UNIQUE,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            kind TEXT NOT NULL CHECK (kind IN ('once', 'weekly')),
            when_utc TEXT,
            local_time TEXT,
            weekdays TEXT,
            CHECK (id IN ('alarm-01','alarm-02','alarm-03','alarm-04','alarm-05',
                          'alarm-06','alarm-07','alarm-08','alarm-09','alarm-10'))
        );
        CREATE TABLE alarm_occurrences (
            occurrence_id TEXT PRIMARY KEY,
            alarm_id TEXT NOT NULL,
            instance_id TEXT NOT NULL,
            due_utc TEXT NOT NULL,
            schedule_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN
                ('in_flight', 'completed', 'failed', 'skipped', 'interrupted')),
            detail TEXT NOT NULL,
            claimed_by TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            UNIQUE(instance_id, due_utc)
        );
        CREATE TABLE alarm_schedule_errors (
            instance_id TEXT NOT NULL,
            alarm_id TEXT NOT NULL,
            code TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (instance_id, code)
        );
        """
    )
    db.execute("INSERT INTO alarm_metadata(key, value) VALUES ('timezone_name', 'UTC')")
    db.execute("INSERT INTO alarm_metadata(key, value) VALUES ('scheduler_stop_epoch', '3')")
    db.execute(
        "INSERT INTO alarms(id, instance_id, active_since, name, name_key, enabled, kind, when_utc,"
        " local_time, weekdays) VALUES (?, ?, ?, ?, ?, 1, 'once', ?, NULL, NULL)",
        (
            "alarm-01",
            "11111111-1111-4111-8111-111111111111",
            "2026-11-18T00:00:00+00:00",
            "Alt",
            "alt",
            (NOW + timedelta(hours=2)).isoformat(),
        ),
    )
    db.commit()
    db.close()


def test_v1_database_is_migrated_without_losing_data(tmp_path):
    path = tmp_path / "alarms.sqlite"
    write_v1_database(path)

    store = AlarmStore(path)

    migrated = store.get("alarm-01")
    assert migrated.name == "Alt"
    assert migrated.instance_id == "11111111-1111-4111-8111-111111111111"
    # migration defaults: catch-up on, per-alarm window unset -> scheduler default
    assert migrated.catchup_enabled is True
    assert migrated.catchup_seconds is None
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            db.execute(
                "SELECT value FROM alarm_metadata WHERE key='scheduler_stop_epoch'"
            ).fetchone()[0]
            == "3"
        )


def test_migration_is_idempotent_and_keeps_epoch(tmp_path):
    path = tmp_path / "alarms.sqlite"
    write_v1_database(path)
    AlarmStore(path)
    AlarmStore(path)

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_future_schema_version_fails_closed(tmp_path):
    path = tmp_path / "alarms.sqlite"
    AlarmStore(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 99")
        db.commit()
    with pytest.raises(Exception) as error:
        AlarmStore(path)
    assert "DATABASE" in type(error.value).__name__.upper()


# ------------------------------------------------------------ catch-up API


def test_defaults_are_catch_up_enabled_without_individual_window(tmp_path):
    store = AlarmStore(tmp_path / "a.sqlite")
    alarm = store.create(NOW + timedelta(hours=1), now=NOW)

    assert alarm.catchup_enabled is True
    assert alarm.catchup_seconds is None


def test_individual_window_can_be_set_changed_and_cleared(tmp_path):
    store = AlarmStore(tmp_path / "a.sqlite")
    alarm = store.create(NOW + timedelta(hours=1), catchup_seconds=90, now=NOW)
    assert alarm.catchup_seconds == 90

    changed = store.update(alarm.id, catchup_seconds=600, now=NOW)
    assert changed.catchup_seconds == 600

    cleared = store.update(alarm.id, catchup_seconds=None, now=NOW)
    assert cleared.catchup_seconds is None


def test_catch_up_can_be_disabled_individually(tmp_path):
    store = AlarmStore(tmp_path / "a.sqlite")
    alarm = store.create(NOW + timedelta(hours=1), catchup_enabled=False, now=NOW)

    assert alarm.catchup_enabled is False
    assert store.update(alarm.id, catchup_enabled=True, now=NOW).catchup_enabled is True


@pytest.mark.parametrize("value", [-1, 86401, True, "300", 1.5])
def test_invalid_catch_up_values_are_rejected(tmp_path, value):
    store = AlarmStore(tmp_path / "a.sqlite")
    with pytest.raises(ValidationError):
        store.create(NOW + timedelta(hours=1), catchup_seconds=value, now=NOW)


def test_catch_up_survives_restart(tmp_path):
    path = tmp_path / "a.sqlite"
    store = AlarmStore(path)
    alarm = store.create(
        NOW + timedelta(hours=1), catchup_seconds=120, catchup_enabled=False, now=NOW
    )
    reloaded = AlarmStore(path).get(alarm.id)

    assert (reloaded.catchup_seconds, reloaded.catchup_enabled) == (120, False)


# ------------------------------------------------------- scheduler windows


@pytest.mark.asyncio
async def test_default_window_is_five_minutes(tmp_path):
    store = AlarmStore(tmp_path / "a.sqlite")
    store.create(NOW - timedelta(minutes=4), now=NOW - timedelta(minutes=5))
    player = FakePlayer()

    played = await make_scheduler(store, player).tick(NOW)

    assert [item.status for item in played] == ["completed"]
    assert player.calls == ["wohnzimmer"]


@pytest.mark.asyncio
async def test_older_than_default_window_is_marked_missed_and_silent(tmp_path):
    store = AlarmStore(tmp_path / "a.sqlite")
    store.create(NOW - timedelta(minutes=6), now=NOW - timedelta(minutes=7))
    player = FakePlayer()
    scheduler = make_scheduler(store, player)

    result = await scheduler.tick(NOW)

    assert [(item.status, item.detail) for item in result] == [("skipped", "missed")]
    assert player.calls == []
    assert [item.alarm_id for item in scheduler.missed()] == ["alarm-01"]


@pytest.mark.asyncio
async def test_individual_window_overrides_the_default(tmp_path):
    store = AlarmStore(tmp_path / "a.sqlite")
    store.create(NOW - timedelta(minutes=8), catchup_seconds=900, now=NOW - timedelta(minutes=9))
    player = FakePlayer()

    played = await make_scheduler(store, player).tick(NOW)

    assert [item.status for item in played] == ["completed"]


@pytest.mark.asyncio
async def test_disabled_catch_up_marks_missed_immediately(tmp_path):
    store = AlarmStore(tmp_path / "a.sqlite")
    store.create(NOW - timedelta(seconds=2), catchup_enabled=False, now=NOW - timedelta(seconds=3))
    player = FakePlayer()
    scheduler = make_scheduler(store, player)

    result = await scheduler.tick(NOW)

    assert [(item.status, item.detail) for item in result] == [("skipped", "missed")]
    assert player.calls == []
    assert len(scheduler.missed()) == 1


@pytest.mark.asyncio
async def test_queue_waiting_time_does_not_make_regular_alarms_missed(tmp_path):
    store = AlarmStore(tmp_path / "a.sqlite")
    for offset in range(10):
        store.create(
            NOW - timedelta(seconds=offset),
            name=f"wecker-{offset}",
            now=NOW - timedelta(minutes=1),
        )
    player = FakePlayer(delay=0.05)
    scheduler = make_scheduler(store, player)

    result = await scheduler.tick(NOW)

    assert len(result) == 10
    assert {item.status for item in result} == {"completed"}
    assert scheduler.missed() == []
