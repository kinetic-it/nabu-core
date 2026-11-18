import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nabu_core.alarm_engine import AlarmStore
from nabu_core.dispatch import DispatchResult, SerialDispatcher
from nabu_core.scheduler import Scheduler

UTC = UTC
NOW = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)


class FakePlayer:
    def __init__(self):
        self.started = []
        self.active = 0
        self.maximum = 0
        self.fail = False

    async def play(self, target):
        self.started.append(target)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            if self.fail:
                raise RuntimeError("fake failure")
        finally:
            self.active -= 1

    async def stop(self, target):
        return True


def dispatcher(player):
    return SerialDispatcher(player, allowed_targets={"fake-player"}, max_seconds=1)


def due(store, *, name=None):
    return store.create(NOW, name=name, now=NOW - timedelta(seconds=1))


@pytest.mark.asyncio
async def test_tick_persists_ten_due_occurrences_and_plays_fake_player_serially(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    for number in range(10):
        due(store, name=f"alarm-{number}")
    player = FakePlayer()

    occurrences = await Scheduler(store, dispatcher(player), "fake-player").tick(NOW)

    assert [occurrence.status for occurrence in occurrences] == ["completed"] * 10
    assert player.started == ["fake-player"] * 10
    assert player.maximum == 1
    assert [
        occurrence.status
        for occurrence in Scheduler(store, dispatcher(player), "fake-player").occurrences()
    ] == ["completed"] * 10


@pytest.mark.asyncio
async def test_occurrence_is_durable_inflight_before_fake_player_starts(tmp_path):
    class InspectingPlayer(FakePlayer):
        scheduler: Scheduler | None = None

        async def play(self, target):
            scheduler = self.scheduler
            assert scheduler is not None
            assert [item.status for item in scheduler.occurrences()] == ["in_flight"]
            await super().play(target)

    store = AlarmStore(tmp_path / "alarms.sqlite")
    due(store)
    player = InspectingPlayer()
    scheduler = Scheduler(store, dispatcher(player), "fake-player")
    player.scheduler = scheduler

    occurrences = await scheduler.tick(NOW)

    assert [occurrence.status for occurrence in occurrences] == ["completed"]


@pytest.mark.asyncio
async def test_competing_schedulers_claim_a_due_occurrence_only_once(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    due(store)
    first, second = FakePlayer(), FakePlayer()
    one = Scheduler(store, dispatcher(first), "fake-player")
    two = Scheduler(store, dispatcher(second), "fake-player")

    results = await asyncio.gather(one.tick(NOW), two.tick(NOW))

    assert sum(len(result) for result in results) == 1
    assert len(first.started) + len(second.started) == 1
    assert [occurrence.status for occurrence in one.occurrences()] == ["completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["disabled", "updated", "deleted"])
async def test_alarm_is_revalidated_after_claim_before_fake_dispatch(tmp_path, mutation):
    class MutatingStore(AlarmStore):
        mutation_pending = True

        def get(self, alarm_id):
            if self.mutation_pending:
                self.mutation_pending = False
                if mutation == "disabled":
                    self.disable(alarm_id)
                elif mutation == "updated":
                    self.update(
                        alarm_id, when=NOW + timedelta(hours=1), now=NOW - timedelta(seconds=1)
                    )
                else:
                    self.delete(alarm_id)
            return super().get(alarm_id)

    store = MutatingStore(tmp_path / "alarms.sqlite")
    due(store)
    player = FakePlayer()

    occurrences = await Scheduler(store, dispatcher(player), "fake-player").tick(NOW)

    assert player.started == []
    assert len(occurrences) == 1
    assert occurrences[0].status == "skipped"
    assert Scheduler(store, dispatcher(player), "fake-player").occurrences()[0].status == "skipped"


@pytest.mark.asyncio
async def test_failed_dispatch_is_persisted_once_and_never_replayed(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    due(store)
    player = FakePlayer()
    player.fail = True
    scheduler = Scheduler(store, dispatcher(player), "fake-player")

    first = await scheduler.tick(NOW)
    second = await scheduler.tick(NOW + timedelta(minutes=1))

    assert [occurrence.status for occurrence in first] == ["failed"]
    assert second == []
    assert player.started == ["fake-player"]
    assert scheduler.occurrences()[0].detail == "RuntimeError"


@pytest.mark.asyncio
async def test_recovery_marks_old_inflight_interrupted_without_blocking_new_due_alarm(tmp_path):
    class BlockingDispatcher:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def ring(self, target):
            self.started.set()
            await self.release.wait()
            return DispatchResult("completed")

    store = AlarmStore(tmp_path / "alarms.sqlite")
    due(store, name="old")
    blocking = BlockingDispatcher()
    old_scheduler = Scheduler(store, blocking, "fake-player")
    in_progress = asyncio.create_task(old_scheduler.tick(NOW))
    await blocking.started.wait()
    in_progress.cancel()
    with pytest.raises(asyncio.CancelledError):
        await in_progress

    due(store, name="new")
    player = FakePlayer()
    restarted = Scheduler(store, dispatcher(player), "fake-player")

    # A cancelled tick already persisted a terminal state, so recovery has no
    # abandoned claim left to relabel - and it must not invent one.
    assert restarted.recover_interrupted() == 0
    completed = await restarted.tick(NOW)

    states = {occurrence.alarm_id: occurrence.status for occurrence in restarted.occurrences()}
    assert states["alarm-01"] == "interrupted"
    assert [occurrence.status for occurrence in completed] == ["completed"]
    assert player.started == ["fake-player"]


@pytest.mark.asyncio
async def test_weekly_dst_problem_is_reported_instead_of_silently_skipped(tmp_path):
    from zoneinfo import ZoneInfo

    berlin = ZoneInfo("Europe/Berlin")
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    store.create(datetime(2026, 1, 4, 2, 30, tzinfo=berlin), weekdays={6})

    scheduler = Scheduler(store, dispatcher(FakePlayer()), "fake-player")
    await scheduler.tick(datetime(2026, 3, 28, 12, 0, tzinfo=UTC))
    assert scheduler.diagnostics == [("alarm-01", "nonexistent local time")]


@pytest.mark.asyncio
async def test_new_weekly_never_replays_before_creation(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    store.create(NOW, weekdays={0}, now=NOW)  # Friday; last Monday predates creation
    player = FakePlayer()
    result = await Scheduler(store, dispatcher(player), "fake-player").tick(NOW)
    assert result == []
    assert player.started == []


@pytest.mark.asyncio
async def test_two_schedulers_share_one_global_playback_channel(tmp_path):
    class Slow(FakePlayer):
        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()
            self.event = asyncio.Event()

        async def play(self, target):
            self.started.append(target)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.event.set()
            await self.release.wait()
            self.active -= 1

    store = AlarmStore(tmp_path / "alarms.sqlite")
    due(store, name="a")
    due(store, name="b")
    player = Slow()
    a = Scheduler(store, dispatcher(player), "fake-player")
    b = Scheduler(store, dispatcher(player), "fake-player")
    first = asyncio.create_task(a.tick(NOW))
    await player.event.wait()
    second = asyncio.create_task(b.tick(NOW))
    await asyncio.sleep(0.02)
    assert player.maximum == 1
    player.release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_stop_persists_simultaneous_due_occurrences_as_skipped_and_prevents_replay(tmp_path):
    class BlockingPlayer(FakePlayer):
        def __init__(self):
            super().__init__()
            self.started_event = asyncio.Event()

        async def play(self, target):
            self.started.append(target)
            self.started_event.set()
            await asyncio.Event().wait()

    store = AlarmStore(tmp_path / "alarms.sqlite")
    due(store, name="first")
    due(store, name="second")
    player = BlockingPlayer()
    scheduler = Scheduler(store, dispatcher(player), "fake-player")
    ticking = asyncio.create_task(scheduler.tick(NOW))
    await player.started_event.wait()

    assert await scheduler.stop(NOW) is True
    stopped = await ticking

    assert [item.status for item in stopped] == ["skipped"]
    assert [item.status for item in scheduler.occurrences()] == ["skipped", "skipped"]
    # the adapter confirmed the stop, so the journal must say so as well
    assert {item.detail for item in scheduler.occurrences()} == {
        "stop_confirmed",
        "stopped",
    }
    assert player.started == ["fake-player"]
    assert await scheduler.tick(NOW + timedelta(seconds=1)) == []


@pytest.mark.asyncio
async def test_recovery_refuses_to_mark_live_claim_while_tick_holds_database_lock(tmp_path):
    class BlockingDispatcher:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def ring(self, target):
            self.started.set()
            await self.release.wait()
            return DispatchResult("completed")

    store = AlarmStore(tmp_path / "alarms.sqlite")
    due(store)
    dispatcher = BlockingDispatcher()
    active = Scheduler(store, dispatcher, "fake-player")
    recovery = Scheduler(store, dispatcher, "fake-player")
    ticking = asyncio.create_task(active.tick(NOW))
    await dispatcher.started.wait()

    assert recovery.recover_interrupted() == 0
    assert [item.status for item in recovery.occurrences()] == ["in_flight"]

    dispatcher.release.set()
    assert [item.status for item in await ticking] == ["completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["once", "weekly"])
async def test_stale_due_alarm_is_persisted_skipped_without_playback(tmp_path, kind):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    if kind == "once":
        store.create(NOW - timedelta(seconds=301), now=NOW - timedelta(seconds=302))
    else:
        store.create(
            NOW - timedelta(days=1),
            weekdays={(NOW - timedelta(days=1)).weekday()},
            now=NOW - timedelta(days=2),
        )
    player = FakePlayer()
    scheduler = Scheduler(store, dispatcher(player), "fake-player")

    result = await scheduler.tick(NOW)

    assert [item.status for item in result] == ["skipped"]
    assert result[0].detail == "missed"
    assert player.started == []
    assert await scheduler.tick(NOW + timedelta(seconds=1)) == []


@pytest.mark.asyncio
async def test_recent_weekly_due_after_active_since_still_dispatches(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    store.create(
        NOW - timedelta(seconds=30),
        weekdays={NOW.weekday()},
        now=NOW - timedelta(seconds=31),
    )
    player = FakePlayer()

    result = await Scheduler(store, dispatcher(player), "fake-player").tick(NOW)

    assert [item.status for item in result] == ["completed"]
    assert player.started == ["fake-player"]


@pytest.mark.asyncio
async def test_stop_epoch_from_another_scheduler_fences_active_claim_without_claiming_silence(
    tmp_path,
):
    class BlockingDispatcher:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def ring(self, target):
            self.started.set()
            await self.release.wait()
            return DispatchResult("completed")

        async def stop(self):
            return False

    class StopOnlyDispatcher:
        def __init__(self):
            self.stop_calls = 0
            self.ring_calls = 0

        async def ring(self, target):
            self.ring_calls += 1
            return DispatchResult("completed")

        async def stop(self):
            self.stop_calls += 1
            return False

    store = AlarmStore(tmp_path / "alarms.sqlite")
    due(store)
    active_dispatcher = BlockingDispatcher()
    stopping_dispatcher = StopOnlyDispatcher()
    active = Scheduler(store, active_dispatcher, "fake-player")
    stopper = Scheduler(store, stopping_dispatcher, "fake-player")
    ticking = asyncio.create_task(active.tick(NOW))
    await active_dispatcher.started.wait()

    assert await stopper.stop(NOW) is False
    active_dispatcher.release.set()
    assert [item.status for item in await ticking] == ["skipped"]
    assert stopping_dispatcher.stop_calls == 1
    assert await stopper.tick(NOW + timedelta(seconds=1)) == []
    assert stopping_dispatcher.ring_calls == 0


@pytest.mark.asyncio
async def test_stop_does_not_relabel_stale_due_alarm_as_missed(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    store.create(NOW - timedelta(seconds=301), now=NOW - timedelta(seconds=302))
    player = FakePlayer()
    scheduler = Scheduler(store, dispatcher(player), "fake-player")

    assert await scheduler.stop(NOW) is False
    assert await scheduler.tick(NOW) == []

    occurrence = scheduler.occurrences()[0]
    assert (occurrence.status, occurrence.detail) == ("skipped", "stopped")
    assert player.started == []


@pytest.mark.asyncio
async def test_stop_is_requested_even_if_journal_fails(tmp_path, monkeypatch):
    class Stopper:
        called = False

        async def stop(self):
            self.called = True
            return False

    p = Stopper()
    scheduler = Scheduler(AlarmStore(tmp_path / "db"), p, "fake")

    def fail(now):
        raise OSError("database failed")

    monkeypatch.setattr(scheduler, "_persist_stop", fail)
    with pytest.raises(OSError):
        await scheduler.stop(NOW)
    assert p.called


@pytest.mark.asyncio
async def test_broken_weekly_rule_does_not_block_unrelated_alarm(tmp_path):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Berlin")
    store = AlarmStore(tmp_path / "db", timezone_name="Europe/Berlin")
    store.create(
        datetime(2026, 1, 4, 2, 30, tzinfo=zone), weekdays={6}, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    now = datetime(2026, 3, 28, 12, tzinfo=UTC)
    store.create(now, now=now - timedelta(seconds=1))
    p = FakePlayer()
    scheduler = Scheduler(store, dispatcher(p), "fake-player")
    completed = await scheduler.tick(now)
    assert [x.status for x in completed] == ["completed"]
    assert p.started == ["fake-player"]
    assert scheduler.diagnostics


@pytest.mark.asyncio
async def test_cancelled_tick_persists_terminal_interrupted_occurrence(tmp_path):
    class BlockingPlayer(FakePlayer):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()

        async def play(self, target):
            self.started.append(target)
            self.entered.set()
            await asyncio.Event().wait()

    store = AlarmStore(tmp_path / "alarms.sqlite")
    due(store)
    player = BlockingPlayer()
    scheduler = Scheduler(store, dispatcher(player), "fake-player")
    running = asyncio.create_task(scheduler.tick(NOW))
    await player.entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    persisted = scheduler.occurrences()
    assert len(persisted) == 1
    assert persisted[0].status == "interrupted"
    assert persisted[0].detail.startswith("cancelled")
    assert scheduler.recover_interrupted() == 0


@pytest.mark.asyncio
async def test_dst_schedule_error_survives_scheduler_restart(tmp_path):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Berlin")
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    store.create(
        datetime(2026, 1, 4, 2, 30, tzinfo=zone),
        weekdays={6},
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    scheduler = Scheduler(store, dispatcher(FakePlayer()), "fake-player")
    await scheduler.tick(datetime(2026, 3, 28, 12, tzinfo=UTC))
    assert scheduler.schedule_errors()

    restarted = Scheduler(
        AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin"),
        dispatcher(FakePlayer()),
        "fake-player",
    )
    errors = restarted.schedule_errors()
    assert [(item.alarm_id, item.code) for item in errors] == [("alarm-01", "invalid_local_time")]


@pytest.mark.asyncio
async def test_schedule_errors_are_idempotent_and_removed_with_the_alarm(tmp_path):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Berlin")
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    alarm = store.create(
        datetime(2026, 1, 4, 2, 30, tzinfo=zone),
        weekdays={6},
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    scheduler = Scheduler(store, dispatcher(FakePlayer()), "fake-player")
    await scheduler.tick(datetime(2026, 3, 28, 12, tzinfo=UTC))
    await scheduler.tick(datetime(2026, 3, 28, 12, tzinfo=UTC))
    assert len(scheduler.schedule_errors()) == 1

    store.delete(alarm.id)
    assert scheduler.schedule_errors() == []


@pytest.mark.asyncio
async def test_confirmed_stop_marks_the_occurrence_as_confirmed(tmp_path):
    """A stop the adapter confirms must not stay recorded as unconfirmed."""

    class BlockingPlayer:
        def __init__(self):
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()

        async def play(self, target):
            self.started.set()
            await self.stopped.wait()

        async def stop(self, target):
            self.stopped.set()
            return True

    store = AlarmStore(tmp_path / "a.sqlite")
    player = BlockingPlayer()
    scheduler = Scheduler(
        store,
        SerialDispatcher(player, allowed_targets={"wohnzimmer"}, max_seconds=10),
        "wohnzimmer",
    )
    store.create(NOW + timedelta(minutes=1), now=NOW)

    tick_task = asyncio.create_task(scheduler.tick(NOW + timedelta(minutes=1, seconds=2)))
    await asyncio.wait_for(player.started.wait(), timeout=5)
    confirmed = await scheduler.stop(NOW + timedelta(minutes=1, seconds=3))
    await asyncio.wait_for(tick_task, timeout=5)

    details = store._run_read(
        lambda db: [row["detail"] for row in db.execute("SELECT detail FROM alarm_occurrences")]
    )
    assert confirmed is True
    assert details == ["stop_confirmed"]
