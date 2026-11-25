"""R32.1: Lebenszyklus von Sitzungsziel und Duck-Fenster.

Das Fenster darf eine Sitzung nicht ueberleben: sonst startet ein spaeterer
Wecker am selben Ziel geduckt, obwohl niemand mehr spricht. Geprueft werden
natuerliches Auslaufen, Stopp, Timeout-Pfad und ein neuer Wecker am selben Ziel.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from nabu_core.alarm_engine import AlarmStore
from nabu_core.alarm_session import AlarmSession
from nabu_core.devices import DevicePolicy
from nabu_core.dispatch import DispatchResult, SerialDispatcher
from nabu_core.player import MediaPlayerAdapter, PlaybackStatus
from nabu_core.scheduler import Scheduler

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


async def test_a_natural_session_end_clears_the_session_target():
    client, dispatcher = build()
    assert (await dispatcher.ring(TARGET)).status == "completed"
    assert (await dispatcher.duck(0.6)).status is PlaybackStatus.DUCK_DEFERRED

    dispatcher.session_ended()

    outcome = await dispatcher.duck(0.6)
    assert outcome.status is PlaybackStatus.DUCK_FAILED
    assert outcome.detail == "not_ringing"


async def test_a_new_alarm_on_the_same_target_is_not_ducked():
    client, dispatcher = build(seconds=0.6)
    assert (await dispatcher.ring(TARGET)).status == "completed"
    assert (await dispatcher.duck(5.0)).status is PlaybackStatus.DUCK_DEFERRED
    dispatcher.session_ended()

    task = asyncio.create_task(dispatcher.ring(TARGET))
    assert await wait_until(lambda: client.states[TARGET] == "playing")
    assert client.volumes[TARGET] == 0.3, "der neue Wecker startet mit Ringpegel"
    assert 0.05 not in [level for _, level in client.writes]

    await dispatcher.stop()
    await drain(task)


async def test_a_stop_also_clears_a_pending_duck():
    client, dispatcher = build(seconds=5.0)
    task = asyncio.create_task(dispatcher.ring(TARGET))
    assert await wait_until(lambda: client.states[TARGET] == "playing")
    await dispatcher.stop()
    await drain(task)

    assert (await dispatcher.duck(2.0)).status is PlaybackStatus.DUCK_FAILED


async def test_scheduler_reports_the_end_of_a_session(tmp_path):
    """Auch ohne Stopp endet eine Sitzung: der Scheduler muss das melden."""

    now = datetime(2026, 11, 25, 7, 0, tzinfo=UTC)
    store = AlarmStore(tmp_path / "alarms.sqlite")
    store.create(now, now=now - timedelta(seconds=1))

    class Dispatcher:
        def __init__(self) -> None:
            self.ended = 0

        async def ring(self, target: str) -> DispatchResult:
            await asyncio.sleep(0.05)
            return DispatchResult("completed")

        async def stop(self) -> bool:
            return True

        def session_ended(self) -> None:
            self.ended += 1

    dispatcher = Dispatcher()

    async def state() -> str:
        return "idle"

    def factory(target: str) -> AlarmSession:
        return AlarmSession(
            ring=lambda: dispatcher.ring(target),
            stop=dispatcher.stop,
            state_of=state,
            poll=0.01,
            ring_seconds=0.4,
            max_restarts=1,
        )

    scheduler = Scheduler(store, dispatcher, TARGET, session_factory=factory)
    await scheduler.tick(now)

    assert dispatcher.ended >= 1, "Sitzungsende wurde nicht gemeldet"
    assert scheduler.occurrences()[0].status in {"completed", "failed"}
