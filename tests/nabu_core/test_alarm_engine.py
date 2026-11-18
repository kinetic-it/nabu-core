import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from nabu_core.alarm_engine import (
    AlarmNotFoundError,
    AlarmStore,
    DatabaseCompatibilityError,
    NameConflictError,
    NoFreeSlotError,
    ValidationError,
)

BERLIN = ZoneInfo("Europe/Berlin")
UTC = UTC
NOW = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def absolute(hour=7, minute=30):
    return datetime(2026, 11, 20, hour, minute, tzinfo=BERLIN)


def create_once(store, when=None, **kwargs):
    return store.create(absolute() if when is None else when, now=NOW, **kwargs)


def test_creating_once_alarm_persists_and_allocates_first_stable_id(tmp_path):
    path = tmp_path / "alarms.sqlite"
    store = AlarmStore(path, timezone_name="Europe/Berlin")

    alarm = create_once(store, name="Wake up")

    assert alarm.id == "alarm-01"
    assert alarm.name == "Wake up"
    assert alarm.kind == "once"
    assert alarm.enabled is True
    assert alarm.when_utc == datetime(2026, 11, 20, 6, 30, tzinfo=UTC)
    assert AlarmStore(path, timezone_name="Europe/Berlin").get("alarm-01") == alarm


def test_new_alarm_has_persisted_uuid4_instance_id(tmp_path):
    path = tmp_path / "alarms.sqlite"
    alarm = create_once(AlarmStore(path))

    assert UUID(alarm.instance_id).version == 4
    assert AlarmStore(path).get(alarm.id).instance_id == alarm.instance_id


def test_delete_and_slot_reuse_creates_a_new_instance_id(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    original = create_once(store, name="old")

    store.delete(original.id)
    replacement = create_once(store, absolute(8), name="new")

    assert replacement.id == original.id
    assert replacement.instance_id != original.instance_id


def test_mutations_preserve_instance_id(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    alarm = create_once(store, name="one")

    updated = store.update(alarm.id, name="two")
    enabled = store.disable(alarm.id)
    disabled = store.enable(alarm.id)

    assert {updated.instance_id, enabled.instance_id, disabled.instance_id} == {alarm.instance_id}


def test_relative_time_is_resolved_against_aware_now_and_persisted(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    now = datetime(2026, 11, 20, 7, 0, tzinfo=BERLIN)

    alarm = store.create(timedelta(minutes=25), now=now)

    assert alarm.when_utc == datetime(2026, 11, 20, 6, 25, tzinfo=UTC)


def test_weekly_alarm_persists_local_time_and_weekdays(tmp_path):
    path = tmp_path / "alarms.sqlite"
    alarm = AlarmStore(path, timezone_name="Europe/Berlin").create(
        absolute(8, 5), name="Work", weekdays={0, 2, 4}
    )

    assert alarm.kind == "weekly"
    assert alarm.when_utc is None
    assert alarm.local_time == "08:05:00"
    assert alarm.weekdays == (0, 2, 4)
    assert AlarmStore(path, timezone_name="Europe/Berlin").get(alarm.id) == alarm


def test_timezone_name_is_persisted_and_incompatible_open_is_rejected(tmp_path):
    path = tmp_path / "alarms.sqlite"
    AlarmStore(path, timezone_name="Europe/Berlin")

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT value FROM alarm_metadata WHERE key = 'timezone_name'"
        ).fetchone() == ("Europe/Berlin",)
    with pytest.raises(DatabaseCompatibilityError, match="timezone"):
        AlarmStore(path, timezone_name="UTC")


def test_old_schema_fails_closed_without_data_loss(tmp_path):
    path = tmp_path / "alarms.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE alarms (id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO alarms VALUES ('old-alarm')")

    with pytest.raises(DatabaseCompatibilityError):
        AlarmStore(path)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id FROM alarms").fetchall() == [("old-alarm",)]


def test_update_enable_disable_delete_and_reuse_freed_slot(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    alarm = create_once(store, name="First")

    updated = store.update(alarm.id, when=absolute(9), name="Changed", now=NOW)
    assert updated.name == "Changed"
    assert updated.when_utc == datetime(2026, 11, 20, 8, 30, tzinfo=UTC)
    assert store.disable(alarm.id).enabled is False
    assert store.enable(alarm.id).enabled is True
    store.delete(alarm.id)
    with pytest.raises(AlarmNotFoundError):
        store.get(alarm.id)
    assert create_once(store, absolute(10)).id == "alarm-01"


def test_update_weekly_to_once_requires_an_explicit_instant(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    weekly = store.create(absolute(), weekdays={0})

    with pytest.raises(ValidationError, match="when"):
        store.update(weekly.id, weekdays=None, now=NOW)


def test_names_are_optional_but_casefold_unique(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    create_once(store, name="MORGEN")

    with pytest.raises(NameConflictError) as error:
        create_once(store, absolute(8), name="morgen")

    assert error.value.code == "NAME_CONFLICT"
    assert create_once(store, absolute(9)).name is None


def test_no_free_slot_is_explicit_after_ten_allocations(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    for hour in range(10):
        create_once(store, absolute(hour))

    with pytest.raises(NoFreeSlotError) as error:
        create_once(store, absolute(11))

    assert error.value.code == "NO_FREE_SLOT"
    assert [alarm.id for alarm in store.list()] == [f"alarm-{i:02d}" for i in range(1, 11)]


@pytest.mark.parametrize(
    "when, now, weekdays",
    [
        (datetime(2026, 11, 20, 7, 0), None, None),
        (timedelta(seconds=0), datetime(2026, 11, 20, 7, 0, tzinfo=UTC), None),
        (absolute(), None, {7}),
        (absolute(), None, set()),
    ],
)
def test_invalid_schedule_input_is_rejected_clearly(tmp_path, when, now, weekdays):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")

    with pytest.raises(ValidationError) as error:
        store.create(when, now=now, weekdays=weekdays)

    assert error.value.code == "INVALID_INPUT"


def test_once_in_past_is_rejected_against_injected_now(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")

    with pytest.raises(ValidationError, match="future"):
        store.create(datetime(2026, 10, 31, 23, 59, tzinfo=UTC), now=NOW)


def test_weekly_rejects_nonexistent_and_ambiguous_dst_local_times(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")

    with pytest.raises(ValidationError, match="DST"):
        store.create(datetime(2026, 3, 29, 2, 30, tzinfo=BERLIN), weekdays={6})
    with pytest.raises(ValidationError, match="DST"):
        store.create(datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN), weekdays={6})


def test_absolute_nonexistent_dst_time_is_rejected(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")

    with pytest.raises(ValidationError, match="nonexistent"):
        store.create(datetime(2026, 3, 29, 2, 30, tzinfo=BERLIN), now=NOW)


def test_absolute_dst_fold_is_stored_as_its_explicit_instant(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    before_fold = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    first = store.create(datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=0), now=before_fold)
    second = store.create(datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1), now=before_fold)

    assert first.when_utc == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert second.when_utc == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def test_mutation_returns_its_committed_row_not_a_reused_slot(tmp_path):
    class ReusingGetStore(AlarmStore):
        reused = False

        def get(self, alarm_id):
            if not self.reused:
                self.reused = True
                self.delete(alarm_id)
                create_once(self, absolute(8), name="replacement")
            return super().get(alarm_id)

    store = ReusingGetStore(tmp_path / "alarms.sqlite")
    created = create_once(store, name="original")

    assert created.name == "original"
    assert store.get(created.id).name == "replacement"


def test_commit_failure_never_returns_a_successful_alarm(tmp_path):
    class CommitFailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")

    class CommitFailingStore(AlarmStore):
        fail_commits = False

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fail_commits = True

        def _connect(self):
            connection = super()._connect()
            return CommitFailingConnection(connection) if self.fail_commits else connection

    path = tmp_path / "alarms.sqlite"
    store = CommitFailingStore(path)

    with pytest.raises(sqlite3.OperationalError, match="injected"):
        create_once(store)

    assert AlarmStore(path).list() == []


def test_connections_are_closed_after_reads_and_writes(tmp_path):
    class TrackingConnection:
        def __init__(self, connection, closed):
            self.connection = connection
            self.closed = closed

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.closed.append(True)
            self.connection.close()

    class TrackingStore(AlarmStore):
        def __init__(self, *args, **kwargs):
            self.closed = []
            super().__init__(*args, **kwargs)
            self.closed.clear()

        def _connect(self):
            return TrackingConnection(super()._connect(), self.closed)

    store = TrackingStore(tmp_path / "alarms.sqlite")
    created = create_once(store)
    store.get(created.id)
    store.list()

    assert len(store.closed) == 3


def test_concurrent_creates_are_atomic_and_leave_no_duplicate_slots(tmp_path):
    path = tmp_path / "alarms.sqlite"

    def create(index):
        store = AlarmStore(path)
        try:
            return create_once(store, absolute(index % 24), name=f"job-{index}").id
        except NoFreeSlotError:
            return "NO_FREE_SLOT"

    with ThreadPoolExecutor(max_workers=20) as executor:
        outcomes = list(executor.map(create, range(20)))

    allocated = [outcome for outcome in outcomes if outcome != "NO_FREE_SLOT"]
    assert sorted(allocated) == [f"alarm-{i:02d}" for i in range(1, 11)]
    assert outcomes.count("NO_FREE_SLOT") == 10
    assert len(AlarmStore(path).list()) == 10


def test_missing_id_is_a_clear_domain_error(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")

    with pytest.raises(AlarmNotFoundError) as error:
        store.enable("alarm-99")

    assert error.value.code == "NOT_FOUND"


def test_relative_time_adds_elapsed_utc_across_spring_dst_gap(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    berlin = ZoneInfo("Europe/Berlin")
    before_gap = datetime(2026, 3, 29, 1, 55, tzinfo=berlin)

    alarm = store.create(timedelta(minutes=10), now=before_gap)

    assert alarm.when_utc == datetime(2026, 3, 29, 1, 5, tzinfo=UTC)
    assert alarm.when_utc.astimezone(berlin) == datetime(2026, 3, 29, 3, 5, tzinfo=berlin)


def test_missing_stop_epoch_fails_closed_instead_of_silently_resetting(tmp_path):
    import sqlite3

    path = tmp_path / "alarms.sqlite"
    AlarmStore(path)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM alarm_metadata WHERE key = ?", ("scheduler_stop_epoch",))
    with pytest.raises(DatabaseCompatibilityError):
        AlarmStore(path)
