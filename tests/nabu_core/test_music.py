"""Music pause/resume around an alarm, without stealing a foreign stream."""

import pytest

from nabu_core.music import HaMusicController, MusicToken

ENTITY = "media_player.echo_2_c2709b_media_player"


class Client:
    def __init__(self, state="playing", media="song.mp3", position=42.5, volume=0.35):
        self.state = state
        self.media = media
        self.position = position
        self.volume = volume
        self.calls = []
        self.pause_works = True
        self.play_works = True

    async def get_state(self, entity_id):
        assert entity_id == ENTITY
        return self.state

    async def get_attributes(self, entity_id):
        assert entity_id == ENTITY
        return {
            "media_content_id": self.media,
            "media_position": self.position,
            "volume_level": self.volume,
        }

    async def pause(self, entity_id):
        self.calls.append("pause")
        if self.pause_works:
            self.state = "paused"

    async def play(self, entity_id):
        self.calls.append("play")
        if self.play_works:
            self.state = "playing"


async def test_pauses_playing_music_and_remembers_where_it_was():
    client = Client()
    token = await HaMusicController(client, entity_id=ENTITY).pause()
    assert token == MusicToken(ENTITY, position=42.5, media_id="song.mp3", volume=0.35)
    assert client.calls == ["pause"]


async def test_a_silent_speaker_is_left_alone():
    client = Client(state="idle")
    assert await HaMusicController(client, entity_id=ENTITY).pause() is None
    assert client.calls == []


async def test_a_paused_speaker_is_not_double_paused():
    client = Client(state="paused")
    assert await HaMusicController(client, entity_id=ENTITY).pause() is None
    assert client.calls == []


async def test_a_pause_that_is_not_observed_returns_no_claim():
    client = Client()
    client.pause_works = False
    assert await HaMusicController(client, entity_id=ENTITY).pause() is None
    assert client.calls == ["pause"]


async def test_resume_plays_the_same_media_again():
    client = Client(state="paused")
    controller = HaMusicController(client, entity_id=ENTITY)
    assert await controller.resume(MusicToken(ENTITY, media_id="song.mp3")) is True
    assert client.calls == ["play"]


async def test_resume_never_starts_on_top_of_other_media():
    client = Client(state="paused", media="someone-elses-podcast.mp3")
    controller = HaMusicController(client, entity_id=ENTITY)
    assert await controller.resume(MusicToken(ENTITY, media_id="song.mp3")) is False
    assert client.calls == []


async def test_resume_does_not_restart_music_that_already_plays():
    client = Client(state="playing")
    controller = HaMusicController(client, entity_id=ENTITY)
    assert await controller.resume(MusicToken(ENTITY, media_id="song.mp3")) is False
    assert client.calls == []


async def test_resume_reports_failure_when_the_player_stays_idle():
    client = Client(state="paused")
    client.play_works = False
    controller = HaMusicController(client, entity_id=ENTITY)
    assert await controller.resume(MusicToken(ENTITY, media_id="song.mp3")) is False
    assert client.calls == ["play"]


async def test_seek_is_optional():
    class WithSeek(Client):
        async def seek(self, entity_id, position):
            self.calls.append(("seek", position))

    client = WithSeek(state="paused")
    controller = HaMusicController(client, entity_id=ENTITY)
    assert await controller.seek(MusicToken(ENTITY, position=42.5)) is True
    assert client.calls == [("seek", 42.5)]

    plain = Client(state="paused")
    assert (
        await HaMusicController(plain, entity_id=ENTITY).seek(MusicToken(ENTITY, position=1.0))
        is False
    )


async def test_attributes_are_optional():
    class NoAttributes(Client):
        async def get_attributes(self, entity_id):
            raise RuntimeError("no attributes")

    client = NoAttributes()
    token = await HaMusicController(client, entity_id=ENTITY).pause()
    assert token is not None and token.position is None and token.media_id is None


def test_entity_must_be_a_media_player():
    with pytest.raises(ValueError):
        HaMusicController(Client(), entity_id="switch.lamp")
