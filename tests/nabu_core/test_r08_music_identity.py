"""R08 regression: unidentifiable music must not be resumed blindly."""

from nabu_core.music import HaMusicController, MusicToken


class UnknownMedia:
    def __init__(self):
        self.play_calls = 0

    async def get_state(self, entity_id):
        return "paused"

    async def get_attributes(self, entity_id):
        return {}

    async def play(self, entity_id):
        self.play_calls += 1


async def test_resume_refuses_when_media_identity_disappears():
    client = UnknownMedia()
    controller = HaMusicController(client, entity_id="media_player.lab")
    token = MusicToken(entity_id="media_player.lab", media_id="track-a")
    assert await controller.resume(token) is False
    assert client.play_calls == 0


async def test_resume_refuses_when_original_identity_is_missing():
    client = UnknownMedia()
    controller = HaMusicController(client, entity_id="media_player.lab")
    token = MusicToken(entity_id="media_player.lab")
    assert await controller.resume(token) is False
    assert client.play_calls == 0


async def test_resume_refuses_token_for_another_entity():
    client = UnknownMedia()
    controller = HaMusicController(client, entity_id="media_player.lab")
    token = MusicToken(entity_id="media_player.other", media_id="track-a")
    assert await controller.resume(token) is False
    assert client.play_calls == 0
