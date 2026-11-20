"""Stop policy for streams whose media identity cannot be read.

R08.1 refused every stop in this situation, which kept the alarm ringing (R09 F-01).
Ownership is now decided by "this adapter started it"; a stream nobody here started
stays untouched.
"""

from nabu_core.devices import DevicePolicy
from nabu_core.player import MediaPlayerAdapter, PlaybackStatus, _Ring

TARGET = "media_player.lab"


class UnidentifiedPlayer:
    def __init__(self, state="playing"):
        self.state = state
        self.stop_calls = 0

    async def get_media_id(self, entity_id):
        return None

    async def get_state(self, entity_id):
        return self.state

    async def stop(self, entity_id):
        self.stop_calls += 1
        self.state = "idle"


def adapter(client):
    return MediaPlayerAdapter(
        client, DevicePolicy(allowed={TARGET}, denied=set()), media=("music", "alarm")
    )


async def test_unidentified_playback_started_here_is_stopped():
    client = UnidentifiedPlayer()
    result = await adapter(client)._finish(_Ring(target=TARGET, started=True))
    assert result.status is PlaybackStatus.STOPPED_CONFIRMED
    assert client.stop_calls == 1


async def test_unidentified_playback_not_started_here_is_left_alone():
    client = UnidentifiedPlayer()
    result = await adapter(client)._finish(_Ring(target=TARGET))
    assert result.status is PlaybackStatus.STOP_UNCONFIRMED
    assert result.detail == "media_identity_unavailable"
    assert client.stop_calls == 0
