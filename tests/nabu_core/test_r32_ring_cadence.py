"""R32: Klingelrhythmus aus der Audiodatei statt aus Pegelwechseln.

Der bisherige Zyklus schreibt alle sieben Sekunden 0.3 -> 0.0 -> 0.3. Jede
0.0-Phase beendet am Echo 2 die Wiedergabe, also erzeugt der Zyklus selbst die
Player-Stopps und Neustarts, die er vermeiden soll. Liegt die Pause in der
Audiodatei (4 s Ton + 3 s Stille), braucht der Ring keinen Pegelwechsel.
"""

from __future__ import annotations

import asyncio

import pytest

from nabu_core.devices import DevicePolicy
from nabu_core.player import MediaPlayerAdapter

TARGET = "media_player.alarm_test"


class FileClient:
    """Spielt eine Datei endlicher Laenge; am Ende meldet das Geraet ``idle``."""

    def __init__(self, file_seconds: float, volume: float = 0.3) -> None:
        self.state = "idle"
        self.volume = volume
        self.file_seconds = file_seconds
        self.started_at: float | None = None
        self.writes: list[tuple[str, float]] = []
        self.plays = 0

    async def play_media(self, target: str, media) -> None:  # noqa: ANN001 - test double
        self.plays += 1
        self.state = "playing"
        self.started_at = asyncio.get_running_loop().time()

    async def stop(self, target: str) -> None:
        self.state = "idle"

    async def get_state(self, target: str) -> str:
        if self.state == "playing" and self.started_at is not None:
            if asyncio.get_running_loop().time() - self.started_at >= self.file_seconds:
                self.state = "idle"
        return self.state

    async def get_media_id(self, target: str):  # noqa: ANN201 - test double
        return "alarm_loop_7s.wav" if self.state == "playing" else None

    async def get_volume(self, target: str) -> float:
        return self.volume

    async def set_volume(self, target: str, volume: float) -> None:
        self.writes.append((target, volume))
        self.volume = volume
        if volume <= 0.0:
            self.state = "idle"


async def wait_until(predicate, seconds: float = 2.0) -> bool:  # noqa: ANN001 - test helper
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


async def drain(task: asyncio.Task) -> list[object]:
    return await asyncio.gather(task, return_exceptions=True)


def build(client: FileClient, **kwargs) -> MediaPlayerAdapter:  # noqa: ANN003 - test helper
    options = {
        "media": ("music", "alarm_loop_7s.wav"),
        "max_seconds": 1.2,
        "volume": 0.3,
        "repeat": True,
    }
    options.update(kwargs)
    return MediaPlayerAdapter(client, DevicePolicy(allowed={TARGET}), **options)


async def test_file_based_cadence_needs_no_volume_writes():
    # 0.15 s statt 7 s: die Datei liefert den Rhythmus, der Test laeuft im Zeitraffer.
    client = FileClient(file_seconds=0.15)
    player = build(client)
    task = asyncio.create_task(player.play(TARGET))

    assert await wait_until(lambda: client.plays >= 3, 2.0), client.plays
    assert client.writes == [], client.writes
    assert client.volume == pytest.approx(0.3)

    await player.stop_ring(TARGET)
    await drain(task)


async def test_volume_based_cycle_still_mutes_to_zero():
    """Negativkontrolle: der alte Zyklus erzeugt die 0.0-Phase, die den Player stoppt."""

    client = FileClient(file_seconds=0.15)
    player = build(client, cycle_on=0.15, cycle_off=0.1)
    task = asyncio.create_task(player.play(TARGET))

    assert await wait_until(lambda: any(level == 0.0 for _, level in client.writes), 2.0), (
        client.writes
    )

    await player.stop_ring(TARGET)
    await drain(task)
