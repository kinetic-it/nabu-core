"""R32: das Ducking darf den Wecker nicht abschalten.

Befund R31 am echten Echo 2: eine Absenkung auf 0.0 beendet die Wiedergabe
(``playing`` -> ``idle``). Das Duckfenster war damit 0,099-0,167 s lang und
danach herrschte Stille, bis der Ring neu startete. Diese Tests halten den
Geraeteeffekt als Praemisse fest und pruefen den Duck-Floor.
"""

from __future__ import annotations

import asyncio

import pytest

from nabu_core.devices import DevicePolicy
from nabu_core.player import MediaPlayerAdapter, PlaybackStatus

TARGET = "media_player.alarm_test"
OTHER = "media_player.other_echo"


class EchoClient:
    """Echo 2 im Labor: positiver Pegel aendert nichts, 0.0 beendet die Wiedergabe."""

    def __init__(self, volume: float = 0.3) -> None:
        self.states = {TARGET: "idle", OTHER: "idle"}
        self.volumes = {TARGET: volume, OTHER: 0.2}
        self.media_ids: dict[str, str | None] = {TARGET: None, OTHER: None}
        self.writes: list[tuple[str, float]] = []
        self.plays = 0
        self.stops = 0

    @property
    def state(self) -> str:
        return self.states[TARGET]

    async def play_media(self, target: str, media) -> None:  # noqa: ANN001 - test double
        self.plays += 1
        self.media_ids[target] = media[1]
        self.states[target] = "playing"

    async def stop(self, target: str) -> None:
        self.stops += 1
        self.states[target] = "idle"
        self.media_ids[target] = None

    async def get_state(self, target: str) -> str:
        return self.states[target]

    async def get_media_id(self, target: str):  # noqa: ANN201 - test double
        return self.media_ids[target]

    async def get_volume(self, target: str) -> float:
        return self.volumes[target]

    async def set_volume(self, target: str, volume: float) -> None:
        self.writes.append((target, volume))
        self.volumes[target] = volume
        if volume <= 0.0:
            # Genau das passiert am Echo 2 und war die Ursache des R31-Fehlers.
            self.states[target] = "idle"
            self.media_ids[target] = None


def build(client: EchoClient, **kwargs) -> MediaPlayerAdapter:  # noqa: ANN003 - test helper
    options = {"media": ("music", "alarm.wav"), "max_seconds": 3.0, "volume": 0.3, "repeat": True}
    options.update(kwargs)
    return MediaPlayerAdapter(client, DevicePolicy(allowed={TARGET, OTHER}), **options)


async def wait_until(predicate, seconds: float = 2.0) -> bool:  # noqa: ANN001 - test helper
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


async def drain(task: asyncio.Task) -> list[object]:
    return await asyncio.gather(task, return_exceptions=True)


async def test_zero_volume_ends_the_playback_on_this_device():
    """Negativkontrolle: ohne diese Geraeteeigenschaft pruefen die Tests nichts."""

    client = EchoClient()
    player = build(client)
    task = asyncio.create_task(player.play(TARGET))
    assert await wait_until(lambda: client.state == "playing")

    await client.set_volume(TARGET, 0.0)

    assert client.state == "idle"
    await player.stop_ring(TARGET)
    await drain(task)


async def test_duck_writes_the_floor_and_keeps_the_stream_playing():
    client = EchoClient()
    player = build(client)
    task = asyncio.create_task(player.play(TARGET))
    assert await wait_until(lambda: client.state == "playing")

    outcome = await player.duck(0.6)

    assert outcome.status is PlaybackStatus.DUCK_CONFIRMED
    assert outcome.detail == "reported_volume_0.05"
    assert client.volumes[TARGET] == pytest.approx(0.05)
    assert client.state == "playing", "Ducking darf die Wiedergabe nicht beenden"
    assert all(level > 0 for _, level in client.writes), "0.0 ist kein Mute auf diesem Geraet"

    await player.stop_ring(TARGET)
    await drain(task)


async def test_ring_volume_returns_once_after_the_duck_window():
    client = EchoClient()
    player = build(client)
    task = asyncio.create_task(player.play(TARGET))
    assert await wait_until(lambda: client.state == "playing")

    await player.duck(0.3)
    assert client.volumes[TARGET] == pytest.approx(0.05)

    assert await wait_until(lambda: client.volumes[TARGET] == pytest.approx(0.3), 1.5)
    restores = [level for _, level in client.writes if level == pytest.approx(0.3)]
    assert len(restores) == 1, "genau eine Rueckkehr auf den Ringpegel"
    assert client.state == "playing"

    await player.stop_ring(TARGET)
    await drain(task)


async def test_manual_volume_change_during_the_ring_wins_over_the_duck():
    """Ein von Hand gesetzter Pegel bleibt stehen: der Adapter kaempft nicht dagegen.

    Folge fuer den Alltag: nach einer manuellen Aenderung kann diese Sitzung nicht
    mehr ducken, bis der Wecker neu gestartet wird. Das ist die bewusste Wahl
    "niemanden ueberstimmen" und in R32 dokumentiert.
    """

    client = EchoClient()
    player = build(client)
    task = asyncio.create_task(player.play(TARGET))
    assert await wait_until(lambda: client.state == "playing")

    await client.set_volume(TARGET, 0.6)  # jemand dreht am Regler
    writes_before = list(client.writes)
    outcome = await player.duck(0.3)

    assert outcome.status is PlaybackStatus.DUCK_UNCONFIRMED
    assert outcome.detail == "duck_floor_not_confirmed"
    assert client.volumes[TARGET] == pytest.approx(0.6), "manueller Pegel bleibt"
    assert client.writes == writes_before, "kein Schreibvorgang gegen den Nutzer"

    await player.stop_ring(TARGET)
    await drain(task)


def test_a_duck_floor_of_zero_is_rejected():
    """0.0 ist kein Mute, sondern ein Player-Stopp: der Wert darf nicht durchrutschen."""

    with pytest.raises(ValueError):
        build(EchoClient(), duck_floor=0.0)
    with pytest.raises(ValueError):
        build(EchoClient(), duck_floor=-0.1)


async def test_a_low_ring_volume_gets_a_proportional_floor():
    """Kleine Ringlautstaerken bleiben nutzbar: der Floor wird gedeckelt, nicht abgelehnt."""

    # Ringpegel 0.04 liegt unter dem Standard-Floor 0.05: ohne Deckelung waere das
    # Ducking wirkungslos oder lauter als der Wecker.
    client = EchoClient(volume=0.04)
    player = build(client, volume=0.04, duck_floor=0.05)
    task = asyncio.create_task(player.play(TARGET))
    assert await wait_until(lambda: client.state == "playing")

    outcome = await player.duck(0.4)

    assert outcome.status is PlaybackStatus.DUCK_CONFIRMED
    assert client.volumes[TARGET] == pytest.approx(0.02), "halber Ringpegel als Boden"
    assert client.state == "playing"
    assert await wait_until(lambda: client.volumes[TARGET] == pytest.approx(0.04), 1.5)

    await player.stop_ring(TARGET)
    await drain(task)
