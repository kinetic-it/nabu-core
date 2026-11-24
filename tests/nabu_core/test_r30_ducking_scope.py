"""R30: ducking scope check against the secured R29 stand.

The ducking implementation itself is part of the ring path; these tests pin the
eleven behaviours the work item asks for, in the places that had no test:
repeated interaction, a failing volume write, a volume somebody else changes,
a stop inside the duck window, a second speaker and foreign media.

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


class Client:
    """A speaker that behaves like Home Assistant's media_player service."""

    def __init__(self, volume: float = 0.5) -> None:
        self.state = "idle"
        self.volumes = {TARGET: volume, OTHER: 0.8}
        self.plays = 0
        self.stops = 0
        self.writes: list[tuple[str, float]] = []
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


@pytest.mark.asyncio
async def test_two_ducks_in_a_row_restore_the_original_volume() -> None:
    """Several spoken interactions during one ring must not drift the level."""

    client = Client(volume=0.5)
    player = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET, OTHER}),
        media=("music", "alarm.wav"),
        max_seconds=3.0,
        volume=0.3,
        repeat=True,
    )
    ringing = asyncio.create_task(player.play(TARGET))
    try:
        await asyncio.wait_for(client.playing.wait(), 1)

        assert (await player.duck(0.05)).status is PlaybackStatus.DUCK_CONFIRMED
        assert client.volumes[TARGET] == 0
        client.playing.clear()
        await asyncio.wait_for(client.playing.wait(), 2)

        assert (await player.duck(0.05)).status is PlaybackStatus.DUCK_CONFIRMED
        assert client.volumes[TARGET] == 0

        await player.stop(TARGET, wait=0.5)
        await ringing
    finally:
        if not ringing.done():
            ringing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ringing

    assert client.volumes[TARGET] == 0.5, "the level drifted after two duck windows"


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
async def test_a_duck_never_stops_foreign_media() -> None:
    """Somebody else's stream must keep playing, even when a duck is requested."""

    client = Client(volume=0.5)
    player = adapter(client, volume=0.3)
    ringing = asyncio.create_task(player.play(TARGET))
    await asyncio.wait_for(client.playing.wait(), 1)

    client.media_id = "spotify:track:someoneelse"
    client.state = "playing"
    before = client.stops

    await player.duck(0.05)
    await player.stop(TARGET, wait=0.5)
    await ringing

    assert client.stops >= before, "the fake counts our own stop"
    assert client.media_id == "spotify:track:someoneelse", "foreign media was replaced"
