"""R32: ein Duck-Fenster zwischen zwei Ring-Generationen.

R31 am echten Geraet: ``nabu_core.alarm_duck`` antwortete ``NOT_RINGING``,
waehrend der Wecker hoerbar klingelte. Ursache: ``_target`` wird am Ende jeder
Generation geloescht und der Adapter hat dann keinen lebenden Ring. Die Sitzung
besitzt den Lautsprecher in dieser Pause aber weiter.
"""

from __future__ import annotations

import asyncio

import pytest

from nabu_core.devices import DevicePolicy
from nabu_core.dispatch import SerialDispatcher
from nabu_core.player import MediaPlayerAdapter, PlaybackStatus

TARGET = "media_player.alarm_test"
OTHER = "media_player.other_echo"


class EchoClient:
    def __init__(self, volume: float = 0.3) -> None:
        self.states = {TARGET: "idle", OTHER: "idle"}
        self.volumes = {TARGET: volume, OTHER: 0.2}
        self.media_ids: dict[str, str | None] = {TARGET: None, OTHER: None}
        self.writes: list[tuple[str, float]] = []
        self.plays = 0

    async def play_media(self, target: str, media) -> None:  # noqa: ANN001 - test double
        self.plays += 1
        self.media_ids[target] = media[1]
        self.states[target] = "playing"

    async def stop(self, target: str) -> None:
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
            self.states[target] = "idle"
            self.media_ids[target] = None


async def wait_until(predicate, seconds: float = 2.0) -> bool:  # noqa: ANN001 - test helper
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


async def drain(task: asyncio.Task) -> list[object]:
    return await asyncio.gather(task, return_exceptions=True)


def build(seconds: float = 1.2) -> tuple[EchoClient, SerialDispatcher]:
    client = EchoClient()
    player = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET, OTHER}),
        media=("music", "alarm.wav"),
        max_seconds=seconds,
        volume=0.3,
        repeat=True,
        duck_floor=0.05,
    )
    return client, SerialDispatcher(player, allowed_targets={TARGET, OTHER}, max_seconds=5.0)


async def test_duck_between_generations_is_deferred_and_applied_next_round():
    client, dispatcher = build()
    assert (await dispatcher.ring(TARGET)).status == "completed"
    assert client.states[TARGET] == "idle"  # Generation vorbei, Sitzung laeuft weiter

    outcome = await dispatcher.duck(0.4)

    assert outcome.status is PlaybackStatus.DUCK_DEFERRED
    assert outcome.detail == "ring_generation_paused"

    task = asyncio.create_task(dispatcher.ring(TARGET))
    assert await wait_until(lambda: client.states[TARGET] == "playing")
    assert client.volumes[TARGET] == pytest.approx(0.05), "naechste Generation startet geduckt"

    assert await wait_until(lambda: client.volumes[TARGET] == pytest.approx(0.3), 1.5)
    await dispatcher.stop()
    await drain(task)


async def test_an_expired_duck_window_is_not_applied_late():
    client, dispatcher = build()
    assert (await dispatcher.ring(TARGET)).status == "completed"

    outcome = await dispatcher.duck(0.05)
    assert outcome.status is PlaybackStatus.DUCK_DEFERRED
    await asyncio.sleep(0.15)

    task = asyncio.create_task(dispatcher.ring(TARGET))
    assert await wait_until(lambda: client.states[TARGET] == "playing")
    assert client.volumes[TARGET] == pytest.approx(0.3), "abgelaufenes Fenster wird verworfen"
    assert all(level > 0 for _, level in client.writes)

    await dispatcher.stop()
    await drain(task)


async def test_no_duck_after_the_alarm_was_stopped():
    client, dispatcher = build(seconds=5.0)
    task = asyncio.create_task(dispatcher.ring(TARGET))
    assert await wait_until(lambda: client.states[TARGET] == "playing")

    await dispatcher.stop()
    await drain(task)

    outcome = await dispatcher.duck(2.0)

    assert outcome.status is PlaybackStatus.DUCK_FAILED
    assert outcome.detail == "not_ringing"


async def test_a_duck_window_never_moves_to_another_speaker():
    client, dispatcher = build()
    assert (await dispatcher.ring(TARGET)).status == "completed"
    assert (await dispatcher.duck(0.6)).status is PlaybackStatus.DUCK_DEFERRED

    task = asyncio.create_task(dispatcher.ring(OTHER))
    assert await wait_until(lambda: client.states[OTHER] == "playing")
    other_levels = [level for target, level in client.writes if target == OTHER]
    assert other_levels == [0.3], other_levels
    assert 0.05 not in other_levels, "das Duck-Fenster gilt nicht fuer ein anderes Ziel"

    await dispatcher.stop()
    await drain(task)
