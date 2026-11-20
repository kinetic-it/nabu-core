"""Media identity: normalise what the player reports, stop only what we started.

R08.1 refused every stop whose media identifier it could not read and aborted a
ring whose identifier was spelled differently (R09 audit findings F-01/F-02). Both
turned into "the alarm keeps ringing", which is the worse failure for an alarm
clock. The rule now: a demonstrably different track is never stopped, an
unreadable identifier does not block a stop of playback this adapter started.
"""

import asyncio

import pytest

from nabu_core.devices import DevicePolicy
from nabu_core.player import (
    IDENTITY_FOREIGN,
    IDENTITY_MATCH,
    IDENTITY_UNKNOWN,
    MediaPlayerAdapter,
    PlaybackStatus,
    _Ring,
    media_identity,
    normalise_media_id,
)

TARGET = "media_player.lab"
ALARM = "media-source://media_source/local/alarm.wav"


def adapter(client, **kwargs):
    return MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}, denied=set()),
        media=("music", ALARM),
        **kwargs,
    )


class SilentPlayer:
    """A player that never reports a media identifier, like the Echo in the field."""

    def __init__(self, state="idle"):
        self.state = state
        self.stops = 0
        self.plays = 0

    async def play_media(self, entity_id, media):
        self.plays += 1
        self.state = "playing"

    async def stop(self, entity_id):
        self.stops += 1
        self.state = "idle"

    async def get_state(self, entity_id):
        return self.state


class ReportingPlayer(SilentPlayer):
    def __init__(self, reported, state="idle"):
        super().__init__(state=state)
        self.reported = reported

    async def get_media_id(self, entity_id):
        return self.reported


async def wait_until_playing(client, budget=2.0):
    """Wait until the adapter's own playback is running, never assume a sleep."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while loop.time() < deadline:
        if client.plays and client.state == "playing":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("playback did not start")


@pytest.mark.parametrize(
    "reported",
    [
        "media-source://media_source/local/alarm.wav",
        "media-source://local/alarm.wav",
        "media-source:/media_source/local/alarm.wav",
        "media-source://media_source//local/alarm.wav",
        "media_source://media_source/local/alarm.wav",
        "/local/alarm.wav",
        "local/alarm.wav",
        "http://homeassistant.local:8123/local/alarm.wav",
        "media-source://media_source/local/alarm%2Ewav",
    ],
)
def test_variants_of_the_same_file_are_one_identity(reported):
    assert media_identity(reported, ALARM) is IDENTITY_MATCH


@pytest.mark.parametrize(
    "reported",
    [
        "media-source://media_source/local/other.wav",
        "http://homeassistant.local:8123/local/other.wav",
        "spotify:track:abc",
    ],
)
def test_other_tracks_stay_foreign(reported):
    assert media_identity(reported, ALARM) is IDENTITY_FOREIGN


@pytest.mark.parametrize("reported", [None, "", "   "])
def test_nothing_to_compare_is_unknown(reported):
    assert media_identity(reported, ALARM) is IDENTITY_UNKNOWN


def test_normalisation_is_idempotent():
    once = normalise_media_id(ALARM)
    assert once == "local/alarm.wav"
    assert normalise_media_id(once) == once


async def test_absent_media_id_still_stops_our_own_alarm():
    client = SilentPlayer()
    player = adapter(client)
    task = asyncio.create_task(player.play(TARGET))
    await wait_until_playing(client)
    result = await player.stop(TARGET)
    await asyncio.wait_for(task, 2)
    assert result.status is PlaybackStatus.STOPPED_CONFIRMED
    assert client.stops == 1
    assert client.state == "idle"


@pytest.mark.parametrize(
    "reported",
    [
        "media-source://local/alarm.wav",
        "media-source://media_source//local/alarm.wav",
        "http://homeassistant.local:8123/local/alarm.wav",
    ],
)
async def test_normalised_identity_rings_and_stops(reported):
    client = ReportingPlayer(reported)
    player = adapter(client)
    task = asyncio.create_task(player.play(TARGET))
    await wait_until_playing(client)
    result = await player.stop(TARGET)
    await asyncio.wait_for(task, 2)
    assert result.status is PlaybackStatus.STOPPED_CONFIRMED
    assert client.stops == 1


async def test_foreign_media_is_not_stopped_when_nothing_was_started():
    client = ReportingPlayer("media-source://media_source/local/other.wav", state="playing")
    player = adapter(client)
    result = await player._finish(_Ring(target=TARGET))
    assert result.status is PlaybackStatus.STOP_UNCONFIRMED
    assert result.detail == "media_replaced"
    assert client.stops == 0


async def test_unreadable_identity_is_not_a_licence_for_a_foreign_stop():
    client = SilentPlayer(state="playing")
    player = adapter(client)
    result = await player._finish(_Ring(target=TARGET))
    assert result.status is PlaybackStatus.STOP_UNCONFIRMED
    assert result.detail == "media_identity_unavailable"
    assert client.stops == 0


async def test_foreign_media_after_our_start_is_neither_stopped_nor_rewritten():
    client = ReportingPlayer(ALARM)
    player = adapter(client, max_seconds=5)
    task = asyncio.create_task(player.play(TARGET))
    await wait_until_playing(client)
    # Somebody replaces the alarm while it is ringing.
    client.reported = "media-source://media_source/local/other.wav"
    with pytest.raises(RuntimeError, match="alarm media replaced"):
        await asyncio.wait_for(task, 2)
    assert client.stops == 0
    assert client.state == "playing"  # the foreign stream was left alone
