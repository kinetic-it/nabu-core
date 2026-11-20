"""The journal must keep *why* an occurrence failed (F-08).

Field finding (R09.3): a ring that died wrote ``detail=RuntimeError`` — the class
name only. Every post-mortem therefore became a guess, and the F-09 race could only
be reconstructed from HA history instead of from the alarm database. The detail now
carries the message, collapsed to one line and truncated, so the column stays
readable and bounded.
"""

from datetime import UTC, datetime, timedelta

from nabu_core.alarm_engine import AlarmStore
from nabu_core.scheduler import Scheduler, describe_failure

TARGET = "media_player.lab"


def test_class_and_message_are_both_kept() -> None:
    assert (
        describe_failure(RuntimeError("playback not confirmed: state=idle"))
        == "RuntimeError: playback not confirmed: state=idle"
    )


def test_message_is_collapsed_to_one_line_and_truncated() -> None:
    text = describe_failure(RuntimeError("a\n b\tc" + "x" * 500))

    assert text.startswith("RuntimeError: a b c")
    assert "\n" not in text and "\t" not in text
    assert len(text) <= 200
    assert text.endswith("...")


def test_empty_message_falls_back_to_the_class_name() -> None:
    assert describe_failure(RuntimeError()) == "RuntimeError"
    assert describe_failure(RuntimeError("   ")) == "RuntimeError"


def test_chained_error_does_not_leak_traceback_text() -> None:
    try:
        try:
            raise ValueError("inner detail")
        except ValueError as error:
            raise RuntimeError("outer detail") from error
    except RuntimeError as error:
        assert describe_failure(error) == "RuntimeError: outer detail"


async def test_failed_occurrence_records_the_reason(tmp_path) -> None:
    now = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    store.create(now, now=now - timedelta(seconds=1))

    class BrokenDispatcher:
        async def ring(self, target):
            raise RuntimeError("alarm media replaced by other content")

        async def stop(self):
            return True

    scheduler = Scheduler(store, BrokenDispatcher(), TARGET)
    finished = await scheduler.tick(now)

    assert [occurrence.status for occurrence in finished] == ["failed"]
    assert finished[0].detail == "RuntimeError: alarm media replaced by other content", (
        "the journal must contain the reason, not only the class name"
    )


async def test_failed_occurrence_stays_readable_after_a_reload(tmp_path) -> None:
    # The detail has to survive a restart in the same shape: no timestamps, no
    # memory addresses, no traceback.
    now = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)
    path = tmp_path / "alarms.sqlite"
    store = AlarmStore(path, timezone_name="Europe/Berlin")
    store.create(now, now=now - timedelta(seconds=1))

    class BrokenDispatcher:
        async def ring(self, target):
            raise TimeoutError("state read timed out")

        async def stop(self):
            return True

    await Scheduler(store, BrokenDispatcher(), TARGET).tick(now)

    reloaded = AlarmStore(path, timezone_name="Europe/Berlin")
    after_restart = Scheduler(reloaded, BrokenDispatcher(), TARGET)
    details = [occurrence.detail for occurrence in after_restart.occurrences()]
    assert details == ["TimeoutError: state read timed out"]
