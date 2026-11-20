"""The real Echo attribute set must be modelled by a test (R09-Audit F-08).

Audit F-08 (R09-C01) complained that the 616 lab tests never model the attribute set
the real device reports, so their evidence weight was limited. The T0 hardware run
settled what that set is: while playing, the Echo 2 reports only

    volume_level, is_volume_muted, device_class, friendly_name, supported_features

— no ``media_content_id``, no title, no source, nothing that names the file
(``T0_MEDIA_ID=ABSENT``). These tests pin exactly that shape.
"""

import pytest

from nabu_core.devices import DevicePolicy
from nabu_core.player import IDENTITY_UNKNOWN, MediaPlayerAdapter, media_identity

TARGET = "media_player.echo_2_c2709b_media_player"
ALARM = "media-source://media_source/local/nabu_audio_test_2s.wav"

# Captured from the device during T0 (R09-C02-R01); nothing shortened or invented.
T0_ATTRIBUTES = {
    "volume_level": 0.5,
    "is_volume_muted": False,
    "device_class": "speaker",
    "friendly_name": "Echo 2 c2709b Media Player",
    "supported_features": 1184268,
}


class EchoLikeClient:
    """Reports exactly what the Echo reports: a state and a volume, no media id."""

    def __init__(self, *, state="idle", volume=0.5, duration=2.0):
        self.state = state
        self.volume = volume
        self.duration = duration
        self.plays = 0
        self.stops = 0
        self.volumes: list[float] = []

    @property
    def attributes(self) -> dict[str, object]:
        # The Echo never adds a media identifier, even while it plays.
        return {**T0_ATTRIBUTES, "volume_level": self.volume}

    async def play_media(self, entity_id, media):
        self.plays += 1
        self.state = "playing"

    async def stop(self, entity_id):
        self.stops += 1
        self.state = "idle"

    async def get_state(self, entity_id):
        return self.state

    async def get_volume(self, entity_id):
        return self.attributes["volume_level"]

    async def set_volume(self, entity_id, level):
        self.volume = float(level)
        self.volumes.append(self.volume)


def make_adapter(client, **kwargs):
    adapter = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}, denied=set()),
        media=("audio/wav", ALARM),
        **kwargs,
    )
    adapter._start_timeout = 0.05
    adapter._stop_timeout = 0.5
    return adapter


def test_missing_media_id_is_unknown_not_foreign() -> None:
    # The whole point of T0: silence is not evidence of foreign content.
    assert media_identity(None, ALARM) == IDENTITY_UNKNOWN
    assert media_identity("", ALARM) == IDENTITY_UNKNOWN
    assert media_identity(T0_ATTRIBUTES.get("media_content_id"), ALARM) == IDENTITY_UNKNOWN


def test_t0_attribute_set_contains_no_media_identifier() -> None:
    # Guards the premise: if this ever changes, the identity policy may be revisited.
    assert not {"media_content_id", "media_title", "media_artist"} & set(T0_ATTRIBUTES)


async def test_alarm_with_the_real_attribute_set_rings_and_stops():
    client = EchoLikeClient(volume=0.5)
    adapter = make_adapter(client, max_seconds=0.4, volume=0.3)

    outcome = await adapter.play(TARGET)

    assert outcome.status == "stopped_confirmed"
    assert client.plays == 1
    assert client.stops == 1, "a speaker that cannot name its media must still be stoppable"
    assert client.volume == pytest.approx(0.5), "the pre-alarm volume is restored"
    assert 0.3 in client.volumes, "the alarm volume was actually applied"


async def test_playing_echo_without_media_id_is_never_treated_as_foreign(caplog):
    client = EchoLikeClient(volume=0.5)
    adapter = make_adapter(client, max_seconds=0.3, volume=0.3)

    outcome = await adapter.play(TARGET)

    assert "replaced" not in (outcome.detail or "")
    assert "foreign" not in " ".join(record.getMessage() for record in caplog.records).lower()


async def test_audio_type_is_sent_to_the_echo():
    # T0 confirmed audio/wav playback; the adapter must not invent another type.
    seen = {}

    class Recording(EchoLikeClient):
        async def play_media(self, entity_id, media):
            seen["media"] = media
            await super().play_media(entity_id, media)

    client = Recording()
    adapter = make_adapter(client, max_seconds=0.2)

    await adapter.play(TARGET)

    assert seen["media"] == ("audio/wav", ALARM)
