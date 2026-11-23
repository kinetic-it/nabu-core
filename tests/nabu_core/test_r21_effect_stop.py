"""R21: Stopp-Bestaetigung ohne Medienidentitaet (D5).

R20-Befund (T5): Der Ring endete hoerbar, der Player war still, im Journal stand
aber ``stop_requested_unconfirmed`` - weil die Medienidentitaet auf dem Echo nicht
lesbar ist (``Alarm media identifier unreadable``) und der Stopp deshalb nie als
bestaetigt galt.

Diese Tests halten die Grenze fest und belegen die Alternative im Sitzungsmodell:

* Bewiesen wird der **Effekt** unseres eigenen ``play_media`` (der Zielplayer hat
  nach unserer Anforderung nachweislich gespielt), nicht die Medienidentitaet.
* Nur wenn dieser Effekt im selben Zeitfenster beobachtet wurde, darf ein stummer
  Player als "gestoppt" gelten.
* Fremde Wiedergabe, die wir nie gestartet haben, wird niemals angefasst.
"""

from __future__ import annotations

import asyncio

from nabu_core.devices import DevicePolicy
from nabu_core.player import MediaPlayerAdapter, PlaybackStatus, _Ring

TARGET = "media_player.lab"
ALARM = "media-source://media_source/local/alarm.wav"


def make_adapter(client) -> MediaPlayerAdapter:
    adapter = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}, denied=set()),
        media=("audio/wav", ALARM),
    )
    adapter._start_timeout = 0.15
    return adapter


class FakePlayer:
    def __init__(self, state: str = "idle") -> None:
        self.state = state
        self.plays = 0
        self.stops = 0
        self.volume = 0.5

    async def play_media(self, entity_id: str, media) -> None:  # noqa: ANN001
        self.plays += 1
        self.state = "playing"

    async def stop(self, entity_id: str) -> None:
        self.stops += 1
        self.state = "idle"

    async def get_state(self, entity_id: str) -> str:
        return self.state

    async def get_volume(self, entity_id: str) -> float:
        return self.volume

    async def set_volume(self, entity_id: str, level: float) -> None:
        self.volume = level


async def test_our_own_ring_that_went_silent_counts_as_stopped() -> None:
    player = FakePlayer()
    adapter = make_adapter(player)

    await adapter.start(TARGET)
    confirmed = await adapter.confirm_playing(TARGET, wait=0.3)
    assert confirmed.status is PlaybackStatus.PLAYING_CONFIRMED

    # The wake word ducks the ring; playback ends before a stop was ever sent.
    player.state = "idle"
    ring = _Ring(target=TARGET)
    ring.play_attempted = True  # confirmed start never ran

    outcome = await adapter._finish(ring)

    assert outcome.status is PlaybackStatus.STOPPED_CONFIRMED
    assert outcome.detail == "already_idle_after_effect"


async def test_foreign_playback_is_never_stopped() -> None:
    player = FakePlayer(state="playing")
    adapter = make_adapter(player)

    ring = _Ring(target=TARGET)
    ring.play_attempted = True  # we tried, but nothing of ours was ever observed

    outcome = await adapter._finish(ring)

    assert outcome.status is PlaybackStatus.STOP_UNCONFIRMED
    assert player.stops == 0, "fremde Wiedergabe darf nie gestoppt werden"


async def test_effect_requires_a_recent_play_request() -> None:
    """A playing state alone is not an effect; it must follow our own request."""
    player = FakePlayer()
    adapter = make_adapter(player)

    adapter._note_effect(TARGET)  # no play request was made
    assert adapter._recent_effect(TARGET) is False

    await adapter.start(TARGET)
    await adapter.confirm_playing(TARGET, wait=0.3)
    assert adapter._recent_effect(TARGET) is True


async def test_effect_expires() -> None:
    player = FakePlayer()
    adapter = make_adapter(player)
    await adapter.start(TARGET)
    await adapter.confirm_playing(TARGET, wait=0.3)

    adapter._effect_at[TARGET] = asyncio.get_running_loop().time() - 400.0
    assert adapter._recent_effect(TARGET, window=300.0) is False
