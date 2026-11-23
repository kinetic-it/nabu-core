"""R22: Ringpfad - Zuordnung ueber Generationen, fremde Medien bleiben unberuehrt.

Produktivbefund 2026-11-23 (T5, HAOS/Pilot): Der Stopp endete nach 50 ms mit
``confirmed=False``; die Occurrence blieb ``stop_requested_unconfirmed``, obwohl
der Ring hoerbar endete. Ursache: Zwischen zwei Ring-Generationen ist
``MediaPlayerAdapter._ring`` None, ``stop_ring`` antwortete deshalb sofort mit
``no_owned_playback`` - ohne den Player ueberhaupt zu pruefen.

Diese Tests halten die Grenzen fest:

* Eine Sitzung darf ihren eigenen Ring auch zwischen Generationen stoppen.
* Ohne eigene, bestaetigte Wiedergabe wird nie ein fremder Stream gestoppt.
* Nach einem Stopp ist die Zuordnung beendet und reicht nicht weiter.
"""

from __future__ import annotations

from nabu_core.devices import DevicePolicy
from nabu_core.player import MediaPlayerAdapter, PlaybackStatus, _Ring

TARGET = "media_player.lab"
ALARM = "media-source://media_source/local/alarm.wav"


class FakePlayer:
    """Echo-aehnlich: kennt keine Medienidentitaet."""

    def __init__(self, state: str = "idle") -> None:
        self.state = state
        self.plays = 0
        self.stops = 0
        self.volume = 0.3

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


def make_adapter(client) -> MediaPlayerAdapter:
    adapter = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}, denied=set()),
        media=("audio/wav", ALARM),
    )
    adapter._start_timeout = 0.15
    adapter._stop_timeout = 0.15
    return adapter


async def _own_playback(client) -> MediaPlayerAdapter:
    adapter = make_adapter(client)
    await adapter.start(TARGET)
    confirmed = await adapter.confirm_playing(TARGET, wait=0.3)
    assert confirmed.status is PlaybackStatus.PLAYING_CONFIRMED
    return adapter


async def test_stop_between_generations_is_confirmed() -> None:
    """Der Ring laeuft weiter, aber _ring ist None - der Stopp muss greifen."""
    client = FakePlayer()
    adapter = await _own_playback(client)

    adapter._ring = None  # Generationenwechsel: zwischen zwei Runden
    client.state = "playing"  # Ringtoene laufen weiter

    outcome = await adapter.stop_ring(TARGET)

    assert outcome.status is PlaybackStatus.STOPPED_CONFIRMED, outcome
    assert client.stops == 1
    assert client.state == "idle"


async def test_foreign_media_is_never_stopped_without_ownership() -> None:
    """Ohne eigene bestaetigte Wiedergabe bleibt fremder Ton unangetastet."""
    client = FakePlayer(state="playing")
    adapter = make_adapter(client)
    adapter._ring = None

    outcome = await adapter.stop_ring(TARGET)

    assert outcome.status is PlaybackStatus.STOP_UNCONFIRMED
    assert outcome.detail == "no_owned_playback"
    assert client.stops == 0, "fremde Wiedergabe darf nie gestoppt werden"


async def test_ownership_ends_with_the_stop() -> None:
    client = FakePlayer()
    adapter = await _own_playback(client)
    adapter._ring = None

    first = await adapter.stop_ring(TARGET)
    assert first.status is PlaybackStatus.STOPPED_CONFIRMED

    client.state = "playing"  # danach spielt etwas anderes
    second = await adapter.stop_ring(TARGET)

    assert second.status is PlaybackStatus.STOP_UNCONFIRMED
    assert client.stops == 1, "nach dem Stopp darf kein zweiter Stop gesendet werden"


async def test_stop_with_active_generation_still_confirmed() -> None:
    client = FakePlayer()
    adapter = make_adapter(client)

    ring = _Ring(target=TARGET)
    ring.play_attempted = True
    ring.started = True
    ring.stopping = True
    adapter._ring = ring
    client.state = "idle"

    outcome = await adapter.stop_ring(TARGET)

    assert outcome.status is PlaybackStatus.STOPPED_CONFIRMED
    assert outcome.detail == "already_idle"


async def test_unconfirmed_stop_is_reported_honestly() -> None:
    """Ein Player, der weiter 'playing' meldet, darf nicht als Stopp gelten."""
    client = FakePlayer()
    adapter = await _own_playback(client)
    adapter._ring = None

    async def stubborn_stop(entity_id: str) -> None:
        client.stops += 1
        client.state = "playing"  # ignoriert den Befehl

    client.stop = stubborn_stop  # type: ignore[assignment]

    outcome = await adapter.stop_ring(TARGET)

    assert outcome.status is PlaybackStatus.STOP_UNCONFIRMED
    assert client.stops == 1
