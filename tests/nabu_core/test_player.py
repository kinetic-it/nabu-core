import asyncio

import pytest

from nabu_core.devices import DeviceDenied, DevicePolicy
from nabu_core.player import MediaPlayerAdapter, PlaybackStatus

LIVING_ROOM = "media_player.wohnzimmer_echo"
KITCHEN = "media_player.kuechendot"


class FakeMediaClient:
    def __init__(self, state="idle", states=None, fail=None):
        self.state = state
        self.states = list(states or [])
        self.fail = fail or set()
        self.calls = []
        self.media_id = None          # what Home Assistant reports as media_content_id

    async def play_media(self, entity_id, media):
        self.calls.append(("play_media", entity_id))
        if "play" in self.fail:
            raise ConnectionError("play failed")
        self.media_id = media[1]
        if self.states:
            self.state = self.states.pop(0)

    async def stop(self, entity_id):
        self.calls.append(("stop", entity_id))
        if "stop" in self.fail:
            raise ConnectionError("stop failed")
        self.media_id = None
        if self.states:
            self.state = self.states.pop(0)

    async def get_state(self, entity_id):
        self.calls.append(("get_state", entity_id))
        if "state" in self.fail:
            raise ConnectionError("state unreadable")
        return self.state

    async def get_media_id(self, entity_id):
        self.calls.append(("get_media_id", entity_id))
        if "media" in self.fail:
            raise ConnectionError("media id unreadable")
        return self.media_id


def policy(**kwargs):
    options = {"allowed": {LIVING_ROOM}, "denied": {KITCHEN}}
    options.update(kwargs)
    return DevicePolicy(**options)


def test_only_allowlisted_devices_are_accepted():
    checked = policy()
    checked.check(LIVING_ROOM)

    with pytest.raises(DeviceDenied):
        checked.check("media_player.unknown")
    with pytest.raises(DeviceDenied):
        checked.check("light.wohnzimmer")


def test_kitchen_dot_is_denied_even_if_it_appears_allowed():
    checked = policy(allowed={LIVING_ROOM, KITCHEN})

    with pytest.raises(DeviceDenied) as error:
        checked.check(KITCHEN)
    assert error.value.reason == "explicitly_denied"


def test_empty_allowlist_denies_everything():
    with pytest.raises(DeviceDenied):
        DevicePolicy(allowed=set(), denied=set()).check(LIVING_ROOM)


def test_policy_rejects_sets_given_as_strings():
    with pytest.raises(ValueError):
        DevicePolicy(allowed="media_player.x", denied=set())


@pytest.mark.asyncio
async def test_start_reports_command_acceptance_not_audible_playback():
    client = FakeMediaClient(state="idle")
    adapter = MediaPlayerAdapter(client, policy(), media=("audio/wav", "media-source://x"))

    outcome = await adapter.start(LIVING_ROOM)

    assert outcome.status is PlaybackStatus.ACCEPTED
    assert client.calls == [("play_media", LIVING_ROOM)]


@pytest.mark.asyncio
async def test_playback_start_requires_an_observed_playing_state():
    client = FakeMediaClient(state="idle", states=["playing"])
    adapter = MediaPlayerAdapter(client, policy(), media=("audio/wav", "media-source://x"))

    await adapter.start(LIVING_ROOM)
    outcome = await adapter.confirm_playing(LIVING_ROOM, wait=0.5, interval=0.01)

    assert outcome.status is PlaybackStatus.PLAYING_CONFIRMED


@pytest.mark.asyncio
async def test_missing_playing_state_is_reported_as_unconfirmed():
    client = FakeMediaClient(state="idle")
    adapter = MediaPlayerAdapter(client, policy(), media=("audio/wav", "media-source://x"))

    await adapter.start(LIVING_ROOM)
    outcome = await adapter.confirm_playing(LIVING_ROOM, wait=0.05, interval=0.01)

    assert outcome.status is PlaybackStatus.START_UNCONFIRMED


@pytest.mark.asyncio
async def test_confirmed_stop_requires_a_non_playing_state():
    client = FakeMediaClient(state="playing", states=["idle"])
    adapter = MediaPlayerAdapter(client, policy(), media=("audio/wav", "media-source://x"))

    outcome = await adapter.stop(LIVING_ROOM, wait=0.5, interval=0.01)

    assert outcome.status is PlaybackStatus.STOPPED_CONFIRMED


@pytest.mark.asyncio
async def test_stop_without_state_change_is_not_silence():
    client = FakeMediaClient(state="playing")
    adapter = MediaPlayerAdapter(client, policy(), media=("audio/wav", "media-source://x"))

    outcome = await adapter.stop(LIVING_ROOM, wait=0.05, interval=0.01)

    assert outcome.status is PlaybackStatus.STOP_UNCONFIRMED
    assert "silence" not in outcome.detail.lower()


@pytest.mark.asyncio
async def test_denied_device_never_reaches_the_client():
    client = FakeMediaClient()
    adapter = MediaPlayerAdapter(client, policy(), media=("audio/wav", "media-source://x"))

    with pytest.raises(DeviceDenied):
        await adapter.start(KITCHEN)
    assert client.calls == []


@pytest.mark.asyncio
async def test_client_failure_is_reported_not_swallowed():
    client = FakeMediaClient(fail={"play"})
    adapter = MediaPlayerAdapter(client, policy(), media=("audio/wav", "media-source://x"))

    outcome = await adapter.start(LIVING_ROOM)

    assert outcome.status is PlaybackStatus.FAILED
    assert outcome.detail == "ConnectionError"


@pytest.mark.asyncio
async def test_unreadable_state_keeps_playback_unconfirmed():
    client = FakeMediaClient(fail={"state"})
    adapter = MediaPlayerAdapter(client, policy(), media=("audio/wav", "media-source://x"))

    outcome = await adapter.confirm_playing(LIVING_ROOM, wait=0.05, interval=0.01)

    assert outcome.status is PlaybackStatus.START_UNCONFIRMED


@pytest.mark.asyncio
async def test_gated_player_fits_the_existing_serial_dispatcher():
    from nabu_core.dispatch import SerialDispatcher

    client = FakeMediaClient(state="idle", states=["playing", "idle"])
    adapter = MediaPlayerAdapter(
        client, policy(), media=("audio/wav", "media-source://x"), max_seconds=0.4
    )
    dispatcher = SerialDispatcher(adapter, allowed_targets={LIVING_ROOM}, max_seconds=5)

    result = await dispatcher.ring(LIVING_ROOM)

    assert result.status == "completed"
    assert ("play_media", LIVING_ROOM) in client.calls


@pytest.mark.asyncio
async def test_gated_player_denies_before_any_client_call():
    from nabu_core.dispatch import SerialDispatcher

    client = FakeMediaClient()
    adapter = MediaPlayerAdapter(client, policy(allowed=set()), media=("a", "b"))
    dispatcher = SerialDispatcher(adapter, allowed_targets={LIVING_ROOM}, max_seconds=1)

    result = await dispatcher.ring(LIVING_ROOM)

    assert result.status == "failed"
    assert result.detail == "DeviceDenied"
    assert client.calls == []


@pytest.mark.asyncio
async def test_play_holds_the_ring_phase_for_the_bounded_time():
    """A ring must not end the moment the command was accepted."""
    client = FakeMediaClient(state="idle", states=["playing", "idle"])
    adapter = MediaPlayerAdapter(
        client, policy(), media=("audio/wav", "media-source://x"), max_seconds=0.6
    )

    started = asyncio.get_running_loop().time()
    await adapter.play(LIVING_ROOM)
    elapsed = asyncio.get_running_loop().time() - started

    assert 0.5 <= elapsed < 2.0
    assert ("stop", LIVING_ROOM) in client.calls


@pytest.mark.asyncio
async def test_stop_shortens_a_running_ring():
    client = FakeMediaClient(state="idle", states=["playing", "idle"])
    adapter = MediaPlayerAdapter(
        client, policy(), media=("audio/wav", "media-source://x"), max_seconds=30
    )

    task = asyncio.create_task(adapter.play(LIVING_ROOM))
    await asyncio.sleep(0.2)
    outcome = await adapter.stop(LIVING_ROOM, wait=1.0, interval=0.05)
    await asyncio.wait_for(task, timeout=5)

    assert outcome.status is PlaybackStatus.STOPPED_CONFIRMED
    assert task.done()
