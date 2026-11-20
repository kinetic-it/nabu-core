"""Schema 3 migration and rollback, verified on isolated database copies.

R09 audit F-04: the schema-3 step is written by the plain store setup, and the
previously installed code rejects a schema-3 database ("unexpected table set").
Rollback therefore needs a database, not just a git checkout. These tests pin the
migration, the restore path and the controlled downgrade on throwaway copies.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from nabu_core.alarm_engine import SCHEMA_VERSION, AlarmStore, DatabaseCompatibilityError

LEGACY_TABLES = {
    "alarms",
    "alarm_metadata",
    "alarm_occurrences",
    "alarm_schedule_errors",
}


def _tables(path):
    with sqlite3.connect(path) as db:
        return {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not row[0].startswith("sqlite_")
        }


def _version(path):
    with sqlite3.connect(path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def _integrity(path):
    with sqlite3.connect(path) as db:
        return db.execute("PRAGMA integrity_check").fetchone()[0]


def make_v2_copy(tmp_path):
    """Create a store, then turn the file into the pre-session (schema 2) shape."""
    path = tmp_path / "alarms.sqlite"
    store = AlarmStore(path, timezone_name="Europe/Berlin")
    now = datetime(2026, 11, 20, 7, tzinfo=UTC)
    alarm = store.create(now + timedelta(minutes=1), now=now)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE alarm_sessions")
        db.execute("PRAGMA user_version = 2")
        db.execute("UPDATE alarm_metadata SET value='8' WHERE key='scheduler_stop_epoch'")
    # WAL keeps the change in a sidecar; the copy must be self-contained.
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path, alarm


def test_v2_database_is_the_old_table_set(tmp_path):
    path, _ = make_v2_copy(tmp_path)
    assert _version(path) == 2
    assert _tables(path) == LEGACY_TABLES
    assert LEGACY_TABLES < _tables(path) | {"alarm_sessions"}


def test_migration_writes_schema_three_and_keeps_the_data(tmp_path):
    path, alarm = make_v2_copy(tmp_path)
    migrated = AlarmStore(path, timezone_name="Europe/Berlin")
    assert _version(path) == SCHEMA_VERSION == 3
    assert _tables(path) == LEGACY_TABLES | {"alarm_sessions"}
    assert _integrity(path) == "ok"
    assert migrated.get(alarm.id).instance_id == alarm.instance_id
    epoch = migrated._run_read(
        lambda db: db.execute(
            "SELECT value FROM alarm_metadata WHERE key='scheduler_stop_epoch'"
        ).fetchone()[0]
    )
    assert epoch == "8"


def test_a_schema_three_file_is_not_accepted_as_schema_two(tmp_path):
    """What the previously installed code does: it refuses the extra table."""
    path, _ = make_v2_copy(tmp_path)
    AlarmStore(path, timezone_name="Europe/Berlin")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 2")
    with pytest.raises(DatabaseCompatibilityError):
        AlarmStore(path, timezone_name="Europe/Berlin")


def test_rollback_by_restoring_the_copy_returns_to_schema_two(tmp_path):
    path, _ = make_v2_copy(tmp_path)
    snapshot = path.read_bytes()
    AlarmStore(path, timezone_name="Europe/Berlin")
    assert _version(path) == 3
    # The documented rollback: remove the migrated file (and its sidecars), then
    # restore the copy. A leftover write-ahead log would replay the migration.
    for name in (path.name, path.name + "-wal", path.name + "-shm"):
        candidate = path.with_name(name)
        if candidate.exists():
            candidate.unlink()
    path.write_bytes(snapshot)
    assert _version(path) == 2
    assert _tables(path) == LEGACY_TABLES
    assert _integrity(path) == "ok"
    assert AlarmStore(path, timezone_name="Europe/Berlin").list()


def test_controlled_downgrade_keeps_the_database_usable(tmp_path):
    """Offline downgrade: drop the session table, re-open, migrate again."""
    path, alarm = make_v2_copy(tmp_path)
    AlarmStore(path, timezone_name="Europe/Berlin")
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE alarm_sessions")
        db.execute("PRAGMA user_version = 2")
    again = AlarmStore(path, timezone_name="Europe/Berlin")
    assert _version(path) == 3
    assert _integrity(path) == "ok"
    assert again.get(alarm.id).instance_id == alarm.instance_id
