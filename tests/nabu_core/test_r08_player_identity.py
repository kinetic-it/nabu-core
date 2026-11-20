"""Fail closed when an active media stream has no observable identity."""

from nabu_core.devices import DevicePolicy
from nabu_core.player import MediaPlayerAdapter, PlaybackStatus, _Ring


class UnidentifiedPlayer:
    def __init__(self):
        self.stop_calls = 0

    async def get_media_id(self, entity_id):
        return None

    async def get_state(self, entity_id):
        return "playing"

    async def stop(self, entity_id):
        self.stop_calls += 1


async def test_unidentified_stream_must_not_be_stopped():
    target = "media_player.lab"
    client = UnidentifiedPlayer()
    adapter = MediaPlayerAdapter(
        client, DevicePolicy(allowed={target}, denied=set()), media=("music", "alarm")
    )
    result = await adapter._finish(_Ring(target=target, started=True, media_id="alarm"))
    assert client.stop_calls == 0
    assert result.status is PlaybackStatus.STOP_UNCONFIRMED
    assert result.detail == "media_identity_unavailable"
