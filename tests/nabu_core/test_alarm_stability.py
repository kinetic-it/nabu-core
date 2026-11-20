import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nabu_core.alarm_engine import AlarmStore
from nabu_core.devices import DevicePolicy
from nabu_core.dispatch import SerialDispatcher
from nabu_core.player import MediaPlayerAdapter, PlaybackStatus, _Ring
from nabu_core.scheduler import Scheduler

TARGET = "media_player.alarm_test"


class PlayerClient:
    def __init__(self):
        self.state = "idle"
        self.volume = 0.5
        self.plays = 0
        self.stops = 0
        self.writes = []
        self.fail_play = False
        self.fail_stop = False
        self.ignore_mute = False
        self.playing = asyncio.Event()
        self.media_id = None  # Home Assistant reports media_content_id

    async def play_media(self, target, media):
        assert target == TARGET
        self.plays += 1
        if self.fail_play:
            raise OSError("play refused")
        self.media_id = media[1]
        self.state = "playing"
        self.playing.set()

    async def stop(self, target):
        assert target == TARGET
        self.stops += 1
        if self.fail_stop:
            raise OSError("stop refused")
        if self.state == "playing":
            self.media_id = None
            self.state = "idle"

    async def get_state(self, target):
        assert target == TARGET
        return self.state

    async def get_media_id(self, target):
        assert target == TARGET
        return self.media_id

    async def get_volume(self, target):
        assert target == TARGET
        return self.volume

    async def set_volume(self, target, volume):
        assert target == TARGET
        self.writes.append(volume)
        if not (self.ignore_mute and volume == 0):
            self.volume = volume


def adapter(client, **kwargs):
    return MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}),
        media=("music", "test.wav"),
        max_seconds=0.5,
        **kwargs,
    )


@pytest.mark.parametrize("state", ["unavailable", "unknown", "unexpected"])
async def test_nonobservable_state_does_not_confirm_stop(state):
    client = PlayerClient()
    client.state = state
    assert (await adapter(client).stop(TARGET, wait=0)).status is PlaybackStatus.STOP_UNCONFIRMED


async def test_stop_failure_does_not_turn_into_a_truthy_dispatch_confirmation():
    client = PlayerClient()
    client.fail_stop = True
    dispatch = SerialDispatcher(adapter(client), allowed_targets={TARGET}, max_seconds=1)
    ring = asyncio.create_task(dispatch.ring(TARGET))
    await asyncio.wait_for(client.playing.wait(), 1)
    assert await dispatch.stop() is False
    await ring


async def test_start_error_restores_volume():
    client = PlayerClient()
    client.fail_play = True
    player = adapter(client, volume=0.3)
    with pytest.raises(RuntimeError, match="play_media failed"):
        await player.play(TARGET)
    assert client.volume == 0.5
    assert client.writes == [0.3, 0.5]


async def test_duck_requires_observed_mute():
    client = PlayerClient()
    client.ignore_mute = True
    player = adapter(client, volume=0.3)
    task = asyncio.create_task(player.play(TARGET))
    try:
        await asyncio.wait_for(client.playing.wait(), 1)
        result = await player.duck(0.1)
        assert result.status.value == "duck_unconfirmed"
        assert client.volume == 0.3
    finally:
        await task


async def test_next_tick_recovers_fresh_orphan_without_replaying_it(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    now = datetime.now(UTC)
    abandoned = store.create(now, now=now - timedelta(seconds=2))
    other = store.create(now, now=now - timedelta(seconds=1))
    client = PlayerClient()
    dispatch = SerialDispatcher(adapter(client), allowed_targets={TARGET}, max_seconds=1)
    old = Scheduler(store, dispatch, TARGET)
    lock = old._try_execution_lock()
    assert lock is not None
    claim = old._claim(abandoned, now, old._current_epoch())
    assert claim is not None
    new = Scheduler(store, dispatch, TARGET)
    try:
        assert await new.tick(now) == []
        assert client.plays == 0
    finally:
        old._release_execution_lock(lock)
    await new.tick(now)
    assert {row.alarm_id: row.status for row in new.occurrences()} == {
        abandoned.id: "interrupted",
        other.id: "completed",
    }
    assert client.plays == 1
    await new.tick(now)
    assert client.plays == 1


async def test_automatic_ring_limit_restores_original_volume():
    client = PlayerClient()
    result = await adapter(client, volume=0.3).play(TARGET)
    assert result.status is PlaybackStatus.STOPPED_CONFIRMED
    assert client.writes == [0.3, 0.5]


async def test_user_volume_change_is_not_overwritten_on_stop():
    client = PlayerClient()
    player = adapter(client, volume=0.3)
    task = asyncio.create_task(player.play(TARGET))
    await asyncio.wait_for(client.playing.wait(), 1)
    client.volume = 0.62
    assert (await player.stop(TARGET)).status is PlaybackStatus.STOPPED_CONFIRMED
    await task
    assert client.volume == 0.62
    assert client.writes == [0.3]


async def test_busy_player_is_neither_stopped_nor_adjusted():
    client = PlayerClient()
    client.state = "playing"
    with pytest.raises(RuntimeError, match="busy or unavailable"):
        await adapter(client, volume=0.3).play(TARGET)
    assert client.plays == client.stops == 0
    assert client.writes == []


async def test_volume_restore_survives_cancellation_without_a_second_stop():
    client = PlayerClient()
    player = adapter(client, volume=0.3)
    dispatch = SerialDispatcher(player, allowed_targets={TARGET}, max_seconds=1)
    task = asyncio.create_task(dispatch.ring(TARGET))
    await asyncio.wait_for(client.playing.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.stops == 1
    assert client.writes == [0.3, 0.5]


async def test_unreachable_cleanup_retries_only_after_idle_without_playing(caplog):
    class Disconnecting(PlayerClient):
        async def get_volume(self, target):
            return None if self.state == "unavailable" else self.volume

    client = Disconnecting()
    player = adapter(client, volume=0.3)
    task = asyncio.create_task(player.play(TARGET))
    await asyncio.wait_for(client.playing.wait(), 1)
    # The player drops out for good: the ring recovers by re-issuing the media,
    # so the outage has to reject the play as well to model "unreachable".
    client.state = "unavailable"
    client.fail_play = True
    with pytest.raises(RuntimeError):
        await task
    assert client.volume == 0.3
    assert "restore deferred" in caplog.text
    client.state = "idle"
    client.fail_play = False
    await player.reconcile_volume(TARGET)
    assert client.volume == 0.5
    assert client.plays >= 1
    assert client.stops == 1


async def test_duck_restarts_repetition_and_restores_after_stop():
    client = PlayerClient()
    player = adapter(client, volume=0.3, repeat=True)
    task = asyncio.create_task(player.play(TARGET))
    try:
        await asyncio.wait_for(client.playing.wait(), 1)
        assert (await player.duck(0.05)).status is PlaybackStatus.DUCK_CONFIRMED
        assert client.volume == 0
        client.playing.clear()
        await asyncio.wait_for(client.playing.wait(), 1)
        assert client.plays == 2
        assert client.volume == 0.3
    finally:
        await player.stop(TARGET)
        await task
    assert client.volume == 0.5
    assert client.stops == 1


async def test_stop_racing_mute_never_restarts_the_ring():
    class SlowMute(PlayerClient):
        def __init__(self):
            super().__init__()
            self.muting = asyncio.Event()
            self.release = asyncio.Event()

        async def set_volume(self, target, volume):
            if volume == 0:
                self.muting.set()
                await self.release.wait()
            await super().set_volume(target, volume)

    client = SlowMute()
    player = adapter(client, volume=0.3, repeat=True)
    task = asyncio.create_task(player.play(TARGET))
    await asyncio.wait_for(client.playing.wait(), 1)
    duck = asyncio.create_task(player.duck(0.05))
    await asyncio.wait_for(client.muting.wait(), 1)
    stopper = asyncio.create_task(player.stop(TARGET))
    await asyncio.sleep(0)
    client.release.set()
    assert (await duck).status is PlaybackStatus.DUCK_UNCONFIRMED
    assert (await stopper).status is PlaybackStatus.STOPPED_CONFIRMED
    await task
    assert client.plays == client.stops == 1
    assert client.volume == 0.5


async def test_external_media_replacement_is_not_stopped_or_adjusted():
    class IdentifiedMedia(PlayerClient):
        media_id = "alarm.wav"

        async def get_media_id(self, target):
            return self.media_id

    client = IdentifiedMedia()
    player = adapter(client, volume=0.3, repeat=True)
    task = asyncio.create_task(player.play(TARGET))
    await asyncio.wait_for(client.playing.wait(), 1)
    await asyncio.sleep(0)
    client.media_id = "other.mp3"
    assert (await player.stop(TARGET)).status is PlaybackStatus.STOP_UNCONFIRMED
    await task
    assert client.stops == 0
    assert client.writes == [0.3]


async def test_unconfirmed_stop_remains_unconfirmed_in_durable_journal(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite")
    now = datetime.now(UTC)
    store.create(now, now=now - timedelta(seconds=1))
    client = PlayerClient()
    client.fail_stop = True
    dispatch = SerialDispatcher(adapter(client), allowed_targets={TARGET}, max_seconds=1)
    scheduler = Scheduler(store, dispatch, TARGET)
    running = asyncio.create_task(scheduler.tick(now))
    await asyncio.wait_for(client.playing.wait(), 1)
    assert await scheduler.stop(now) is False
    assert await scheduler.stop(now) is False
    await running
    assert [(row.status, row.detail) for row in scheduler.occurrences()] == [
        ("skipped", "stop_requested_unconfirmed"),
    ]
    await scheduler.tick(now)
    assert client.plays == 1


async def test_play_service_error_after_start_still_stops_owned_alarm():
    class StartsThenRaises(PlayerClient):
        async def play_media(self, target, media):
            await super().play_media(target, media)
            raise OSError("connection lost after start")

        async def get_media_id(self, target):
            return "test.wav"

    client = StartsThenRaises()
    with pytest.raises(RuntimeError, match="play_media failed"):
        await adapter(client, volume=0.3).play(TARGET)
    assert client.stops == 1
    assert client.state == "idle"
    assert client.volume == 0.5


class NoIdentityClient:
    """A player that reports state and volume but no media identifier at all."""

    def __init__(self):
        self._inner = PlayerClient()

    def __getattr__(self, name):
        if name == "get_media_id":
            raise AttributeError(name)
        return getattr(self._inner, name)


async def test_missing_media_identity_still_allows_stopping_our_own_alarm():
    """R09 F-01: an unreadable identifier must not leave the alarm ringing.

    This player exposes no media identifier at all, which is what the target Echo
    does. R08.1 refused the stop and the alarm kept playing; ownership is now
    decided by "this adapter started it", not by an identifier the device never
    reports.
    """
    client = NoIdentityClient()
    player = adapter(client, volume=0.3)
    task = asyncio.create_task(player.play(TARGET))
    await asyncio.wait_for(client.playing.wait(), 1)
    result = await player.stop(TARGET)
    assert result.status is PlaybackStatus.STOPPED_CONFIRMED
    await task
    assert client.stops == 1


async def test_missing_media_identity_does_not_stop_a_foreign_stream():
    """Nothing was started here, so an unreadable identity must protect the stream."""
    client = NoIdentityClient()
    client._inner.state = "playing"  # somebody else's audio on the target
    side = MediaPlayerAdapter(client, DevicePolicy(allowed={TARGET}), media=("music", "test.wav"))
    result = await side._finish(_Ring(target=TARGET))
    assert result.status is PlaybackStatus.STOP_UNCONFIRMED
    assert result.detail == "media_identity_unavailable"
    assert client.stops == 0


async def test_pending_volume_recovery_survives_adapter_restart(tmp_path):
    class Disconnecting(PlayerClient):
        async def get_volume(self, target):
            return None if self.state == "unavailable" else self.volume

    client = Disconnecting()
    path = tmp_path / "recovery.sqlite"
    store = AlarmStore(path)
    player = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}),
        media=("music", "test.wav"),
        max_seconds=0.5,
        volume=0.3,
        recovery_store=store,
    )
    task = asyncio.create_task(player.play(TARGET))
    await asyncio.wait_for(client.playing.wait(), 1)
    # Rejected play: a single dropped state poll is now recovered by re-issuing the
    # media, so an unreachable player has to refuse the call as well.
    client.state = "unavailable"
    client.fail_play = True
    with pytest.raises(RuntimeError):
        await task
    replacement = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}),
        media=("music", "test.wav"),
        max_seconds=0.5,
        volume=0.3,
        recovery_store=AlarmStore(path),
    )
    client.state = "idle"
    await replacement.reconcile_volume(TARGET)
    assert client.volume == 0.5


async def test_volume_attempt_is_durable_before_player_call(tmp_path):
    from nabu_core.volume_journal import VolumeJournal

    store = AlarmStore(tmp_path / "alarms.sqlite")

    class CrashAfterSideEffect(PlayerClient):
        async def set_volume(self, target, volume):
            record = VolumeJournal(store).get(target)
            assert record is not None and record["attempted"] == volume
            await super().set_volume(target, volume)
            raise OSError("response lost")

    client = CrashAfterSideEffect()
    player = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}),
        media=("music", "test.wav"),
        max_seconds=0.5,
        volume=0.3,
        recovery_store=store,
    )
    with pytest.raises(OSError):
        await player.play(TARGET)
    assert VolumeJournal(AlarmStore(tmp_path / "alarms.sqlite")).get(TARGET) is not None


async def test_recovery_does_not_touch_active_player(tmp_path):
    from nabu_core.volume_journal import VolumeJournal

    store = AlarmStore(tmp_path / "alarms.sqlite")
    journal = VolumeJournal(store)
    generation = journal.begin(TARGET, 0.5)
    journal.attempt(TARGET, generation, 0.3)
    journal.confirm(TARGET, generation, 0.3)
    client = PlayerClient()
    client.state, client.volume = "playing", 0.3
    player = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}),
        media=("music", "test.wav"),
        max_seconds=0.5,
        volume=0.3,
        recovery_store=store,
    )
    await player.reconcile_volume(TARGET)
    assert client.writes == []
    assert journal.get(TARGET)["generation"] == generation
    with pytest.raises(RuntimeError, match="previous alarm volume restore is pending"):
        await player.play(TARGET)
