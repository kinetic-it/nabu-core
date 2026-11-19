from concurrent.futures import ThreadPoolExecutor

import pytest

from nabu_core.alarm_engine import AlarmStore
from nabu_core.volume_journal import RecoveryConflict, VolumeJournal


def test_write_ahead_record_survives_restart_and_stale_generation_cannot_clear(tmp_path):
    path = tmp_path / "alarms.sqlite"
    first = VolumeJournal(AlarmStore(path))
    generation = first.begin("media_player.test", 0.5)
    first.attempt("media_player.test", generation, 0.3)
    second = VolumeJournal(AlarmStore(path))
    assert second.get("media_player.test") == {
        "generation": generation,
        "baseline": 0.5,
        "written": None,
        "attempted": 0.3,
    }
    with pytest.raises(RecoveryConflict):
        second.clear("media_player.test", "stale")
    second.confirm("media_player.test", generation, 0.3)
    assert first.get("media_player.test")["written"] == 0.3
    first.clear("media_player.test", generation)
    assert second.get("media_player.test") is None


def test_competing_generations_are_serialized(tmp_path):
    path = tmp_path / "alarms.sqlite"
    a, b = VolumeJournal(AlarmStore(path)), VolumeJournal(AlarmStore(path))

    def attempt(journal):
        try:
            return journal.begin("media_player.test", 0.5)
        except RecoveryConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(attempt, (a, b)))
    assert sum(value is not None for value in values) == 1


@pytest.mark.parametrize("level", [float("nan"), float("inf"), -0.1, 1.1, True, "0.5"])
def test_reject_invalid_volume_without_persisting(tmp_path, level):
    journal = VolumeJournal(AlarmStore(tmp_path / "alarms.sqlite"))
    with pytest.raises(ValueError, match="invalid volume"):
        journal.begin("media_player.test", level)
    assert journal.get("media_player.test") is None


def test_stale_generation_cannot_mutate_new_owner(tmp_path):
    journal = VolumeJournal(AlarmStore(tmp_path / "alarms.sqlite"))
    old = journal.begin("media_player.test", 0.5)
    journal.clear("media_player.test", old)
    current = journal.begin("media_player.test", 0.7)
    with pytest.raises(RecoveryConflict):
        journal.attempt("media_player.test", old, 0.1)
    assert journal.get("media_player.test")["generation"] == current
    assert journal.get("media_player.test")["baseline"] == 0.7
