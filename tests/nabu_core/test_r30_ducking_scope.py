"""R30: ducking scope check against the secured R29 stand.

The ducking implementation itself is part of the ring path; these tests pin the
behaviours the work item asks for, in the places that had no test: repeated
interaction, a failing volume write, a volume somebody else changes, a stop
inside the duck window, a second speaker and foreign media.

Every test drives the real adapter. Only the Home Assistant client is a fake.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from nabu_core.devices import DevicePolicy
from nabu_core.player import MediaPlayerAdapter, PlaybackStatus

TARGET = "media_player.alarm_test"
OTHER = "media_player.other_echo"
FOREIGN_MEDIA = "spotify:track:someoneelse"


class Client:
    """A speaker that behaves like Home Assistant's media_player service."""

    def __init__(self, volume: float = 0.5) -> None:
        self.state = "idle"
        self.volumes = {TARGET: volume, OTHER: 0.8}
        self.plays = 0
        self.stops = 0
        self.writes: list[tuple[str, float]] = []
        #: Every stop command that actually reached the device, with the media
        #: the device reported at that moment.
        self.stop_calls: list[tuple[str, str | None]] = []
        self.fail_write = False
        self.media_id = None
        self.playing = asyncio.Event()

    async def play_media(self, target: str, media) -> None:  # noqa: ANN001 - test double
        self.plays += 1
        self.media_id = media[1]
        if target == TARGET:
            self.state = "playing"
            self.playing.set()

    async def stop(self, target: str) -> None:
        self.stops += 1
        self.stop_calls.append((target, self.media_id))
        if target == TARGET and self.state == "playing":
            self.state = "idle"
            self.media_id = None

    async def get_state(self, target: str) -> str:
        return self.state if target == TARGET else "idle"

    async def get_media_id(self, target: str):  # noqa: ANN201 - test double
        return self.media_id if target == TARGET else None

    async def get_volume(self, target: str) -> float:
        return self.volumes[target]

    async def set_volume(self, target: str, volume: float) -> None:
        self.writes.append((target, volume))
        if self.fail_write:
            raise OSError("volume write refused")
        self.volumes[target] = volume


def adapter(client: Client, **kwargs) -> MediaPlayerAdapter:  # noqa: ANN003 - test helper
    return MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET, OTHER}),
        media=("music", "alarm.wav"),
        max_seconds=0.6,
        **kwargs,
    )


async def wait_until(predicate, seconds: float = 2.0) -> bool:  # noqa: ANN001 - test helper
    """Poll a condition instead of assuming a fixed timing."""

    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


async def wait_for_volume(client: Client, level: float, seconds: float = 2.0) -> bool:
    """Wait until the adapter really wrote ``level`` - never just assume it."""

    return await wait_until(lambda: client.volumes[TARGET] == level, seconds)


async def drain(task: asyncio.Task) -> list[object]:
    """Finish a ring task without hiding the documented abort of a lost medium."""

    return await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_two_ducks_in_a_row_restore_the_original_volume() -> None:
    """Several spoken interactions during one ring must not drift the level.

    Between the two interactions the ring has to be back at the volume it started
    with; only after the stop does the origin level return.
    """

    client = Client(volume=0.5)
    player = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET, OTHER}),
        media=("music", "alarm.wav"),
        max_seconds=5.0,
        volume=0.3,
        repeat=True,
    )
    ringing = asyncio.create_task(player.play(TARGET))
    try:
        assert await wait_for_volume(client, 0.3, 1.0), "the ring volume was never set"
        plays_at_start = client.plays

        first = await player.duck(0.05)
        assert first.status is PlaybackStatus.DUCK_CONFIRMED
        assert client.volumes[TARGET] == 0.0, "the first duck did not mute"

        assert await wait_for_volume(client, 0.3, 2.0), (
            "the ring volume was not restored between the two interactions"
        )
        after_first = plays_at_start + 1
        assert await wait_until(lambda: client.plays >= after_first), (
            "the ring did not resume after the first duck"
        )

        second = await player.duck(0.05)
        assert second.status is PlaybackStatus.DUCK_CONFIRMED
        assert client.volumes[TARGET] == 0.0, "the second duck did not mute"

        assert await wait_for_volume(client, 0.3, 2.0), (
            "the ring volume was not restored after the second interaction"
        )
        assert await wait_until(lambda: client.plays > after_first), (
            "the ring did not resume after the second duck"
        )

        stopped = await player.stop(TARGET, wait=0.5)
        assert stopped.status is PlaybackStatus.STOPPED_CONFIRMED
        await ringing
    finally:
        if not ringing.done():
            ringing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ringing

    assert client.volumes[TARGET] == 0.5, "the origin level was not restored at the end"


@pytest.mark.asyncio
async def test_a_failing_volume_write_does_not_break_the_ring() -> None:
    """A refused volume write is reported, the alarm keeps ringing and clears up."""

    client = Client(volume=0.5)
    player = adapter(client, volume=0.3)
    ringing = asyncio.create_task(player.play(TARGET))
    try:
        await asyncio.wait_for(client.playing.wait(), 1)
        client.fail_write = True

        result = await player.duck(0.05)

        assert result.status is PlaybackStatus.DUCK_FAILED
        assert result.detail == "OSError", "the transport error should stay visible"
        assert client.volumes[TARGET] == 0.3, "a failed write must not fake the mute"
        assert client.state == "playing", "the alarm was silenced by a failed write"

        client.fail_write = False
        await player.stop(TARGET, wait=0.5)
        await ringing
    finally:
        if not ringing.done():
            ringing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ringing

    assert client.state == "idle"


@pytest.mark.asyncio
async def test_a_volume_somebody_else_changed_survives_the_cleanup() -> None:
    """A level the user picked during the ring outranks the adapter's bookkeeping."""

    client = Client(volume=0.5)
    player = adapter(client, volume=0.3)
    ringing = asyncio.create_task(player.play(TARGET))
    await asyncio.wait_for(client.playing.wait(), 1)

    client.volumes[TARGET] = 0.85  # the user turns the speaker up mid-ring

    await player.stop(TARGET, wait=0.5)
    await ringing

    assert client.volumes[TARGET] == 0.85, "the user's own volume was overwritten"


@pytest.mark.asyncio
async def test_a_stop_inside_the_duck_window_restores_the_volume() -> None:
    client = Client(volume=0.5)
    player = adapter(client, volume=0.3)
    ringing = asyncio.create_task(player.play(TARGET))
    await asyncio.wait_for(client.playing.wait(), 1)

    assert (await player.duck(5.0)).status is PlaybackStatus.DUCK_CONFIRMED
    assert client.volumes[TARGET] == 0

    await player.stop(TARGET, wait=0.5)
    await ringing

    assert client.volumes[TARGET] == 0.5, "a stop during the duck window left the speaker muted"
    assert client.state == "idle"


@pytest.mark.asyncio
async def test_a_duck_never_touches_a_second_speaker() -> None:
    client = Client(volume=0.5)
    player = adapter(client, volume=0.3)
    ringing = asyncio.create_task(player.play(TARGET))
    await asyncio.wait_for(client.playing.wait(), 1)

    await player.duck(0.05)
    await player.stop(TARGET, wait=0.5)
    await ringing

    assert client.volumes[OTHER] == 0.8, "another speaker was changed"
    assert all(target == TARGET for target, _ in client.writes)


@pytest.mark.asyncio
async def test_a_duck_without_a_ring_is_refused() -> None:
    client = Client(volume=0.5)
    player = adapter(client, volume=0.3)

    result = await player.duck(0.05)

    assert result.status is PlaybackStatus.DUCK_FAILED
    assert result.detail == "not_ringing"
    assert client.writes == []
    assert client.volumes[TARGET] == 0.5


@pytest.mark.asyncio
async def test_foreign_media_gets_neither_a_stop_nor_a_volume_write() -> None:
    """A demonstrably foreign medium stays untouched - no stop, no volume write.

    D5 is still open, so this is a refusal test: as soon as the player reports a
    different medium, neither the stop path nor the duck path may send anything.
    """

    client = Client(volume=0.5)
    player = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET, OTHER}),
        media=("music", "alarm.wav"),
        max_seconds=5.0,
        volume=0.3,
        repeat=True,
    )
    ringing = asyncio.create_task(player.play(TARGET))
    try:
        assert await wait_for_volume(client, 0.3, 1.0), "the ring volume was never set"
        writes_before = list(client.writes)

        # While the ring runs, the speaker starts a different stream.
        client.media_id = FOREIGN_MEDIA
        client.state = "playing"

        ducked = await player.duck(0.05)
        stopped = await player.stop(TARGET, wait=0.5)
        aborted = await drain(ringing)
    finally:
        if not ringing.done():
            ringing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ringing

    # Both refusals are correct: the duck itself, or the ring loop noticing first.
    assert ducked.status is PlaybackStatus.DUCK_FAILED
    assert ducked.detail in {"media_replaced", "not_ringing"}, ducked.detail
    assert stopped.status is PlaybackStatus.STOP_UNCONFIRMED
    assert stopped.detail == "media_replaced"
    assert client.stop_calls == [], "a stop command reached the speaker holding foreign media"
    assert client.writes == writes_before, "the other stream's volume was changed"
    assert client.volumes[TARGET] == 0.3
    assert client.media_id == FOREIGN_MEDIA, "the other stream was replaced"
    # A ring that lost its medium ends as a refused stop - never as success and
    # never as some unrelated error.
    for outcome in aborted:
        if isinstance(outcome, BaseException):
            assert "media replaced" in str(outcome), outcome
        elif outcome is not None:
            assert outcome.status is PlaybackStatus.STOP_UNCONFIRMED, outcome
            assert outcome.detail == "media_replaced", outcome


@pytest.mark.asyncio
async def test_our_own_media_is_stopped_and_restored() -> None:
    """Negative control: with our own medium the same harness does stop and write.

    Without this control the refusal test above could pass on a fake that never
    records anything.
    """

    client = Client(volume=0.5)
    player = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET, OTHER}),
        media=("music", "alarm.wav"),
        max_seconds=5.0,
        volume=0.3,
        repeat=True,
    )
    ringing = asyncio.create_task(player.play(TARGET))
    try:
        assert await wait_for_volume(client, 0.3, 1.0), "the ring volume was never set"
        assert client.media_id == "alarm.wav"

        ducked = await player.duck(0.05)
        assert ducked.status is PlaybackStatus.DUCK_CONFIRMED
        assert (TARGET, 0.0) in client.writes

        stopped = await player.stop(TARGET, wait=0.5)
        assert stopped.status is PlaybackStatus.STOPPED_CONFIRMED
        await ringing
    finally:
        if not ringing.done():
            ringing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ringing

    assert client.stop_calls == [(TARGET, "alarm.wav")], "our own alarm was not stopped"
    assert client.volumes[TARGET] == 0.5, "the origin level was not restored"
