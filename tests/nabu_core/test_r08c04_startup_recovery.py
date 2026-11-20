"""A left-over volume record must be answered after a restart (F-10).

Field finding (R09.3, T9): a stop that could not be confirmed before shutdown left
``volume_recovery:<target>`` in the database and nobody looked at it again. The
speaker kept the alarm volume until the next alarm — and that alarm had to clear the
record before it could even ring.

``reconcile_all`` runs once per setup and answers every pending record: restore on an
idle player, stay away from a busy one, never raise.
"""

import pytest

from nabu_core.alarm_engine import AlarmStore
from nabu_core.devices import DevicePolicy
from nabu_core.player import MediaPlayerAdapter
from nabu_core.volume_journal import VolumeJournal

TARGET = "media_player.lab"
OTHER = "media_player.kitchen"
ALARM = "media-source://media_source/local/alarm.wav"


class Client:
    def __init__(self, *, state="idle", volume=0.3, unreadable=False):
        self.state = state
        self.volume = volume
        self.unreadable = unreadable
        self.stops = 0

    async def get_state(self, entity_id):
        if self.unreadable:
            raise TimeoutError("player unreachable")
        return self.state

    async def play_media(self, entity_id, media):
        self.state = "playing"

    async def stop(self, entity_id):
        self.stops += 1
        self.state = "idle"

    async def get_volume(self, entity_id):
        return self.volume

    async def set_volume(self, entity_id, level):
        self.volume = float(level)


def make_adapter(client, store, *, allowed=(TARGET,)):
    return MediaPlayerAdapter(
        client,
        DevicePolicy(allowed=set(allowed), denied=set()),
        media=("audio/wav", ALARM),
        volume=0.3,
        recovery_store=store,
    )


def leave_record(store, target=TARGET, baseline=0.5, attempted=0.3):
    journal = VolumeJournal(store)
    generation = journal.begin(target, baseline)
    journal.attempt(target, generation, attempted)
    return journal


async def test_pending_record_is_restored_and_cleared(tmp_path) -> None:
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    journal = leave_record(store)
    client = Client(state="idle", volume=0.3)

    status = await make_adapter(client, store).reconcile_all()

    assert status == {TARGET: "restored"}
    assert client.volume == pytest.approx(0.5), "the pre-alarm volume comes back"
    assert journal.pending() == [], "the record must not stay behind"
    assert client.stops == 0, "recovery never stops audio"


async def test_busy_player_keeps_the_record_and_its_volume(tmp_path) -> None:
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    journal = leave_record(store)
    client = Client(state="playing", volume=0.3)

    status = await make_adapter(client, store).reconcile_all()

    assert status == {TARGET: "deferred"}
    assert client.volume == pytest.approx(0.3), "a foreign stream is not made louder"
    assert journal.pending() == [TARGET], "the record survives for the next attempt"


async def test_unreachable_player_does_not_break_startup(tmp_path) -> None:
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    journal = leave_record(store)
    client = Client(unreadable=True)

    status = await make_adapter(client, store).reconcile_all()

    assert status == {TARGET: "deferred"}
    assert journal.pending() == [TARGET]


async def test_target_outside_the_allowlist_is_never_touched(tmp_path) -> None:
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    journal = leave_record(store, target=OTHER)
    client = Client(state="idle", volume=0.3)

    status = await make_adapter(client, store).reconcile_all()

    assert status == {OTHER: "skipped"}
    assert client.volume == pytest.approx(0.3)
    assert journal.pending() == [OTHER], "a record is never dropped without a restore"


async def test_without_a_journal_nothing_happens(tmp_path) -> None:
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    client = Client(state="idle", volume=0.3)
    adapter = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}, denied=set()),
        media=("audio/wav", ALARM),
    )

    assert await adapter.reconcile_all() == {}
    assert client.volume == pytest.approx(0.3)
    assert VolumeJournal(store).pending() == [], "no store means no records either"


async def test_only_volume_records_are_reported(tmp_path) -> None:
    # Foreign metadata keys must not be mistaken for pending restores.
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    journal = leave_record(store)
    store._run_write(
        lambda db: db.execute("INSERT INTO alarm_metadata(key,value) VALUES ('unrelated','{}')")
    )
    assert journal.pending() == [TARGET]

    status = await make_adapter(Client(), store).reconcile_all()

    assert status == {TARGET: "restored"}
    assert journal.pending() == []
