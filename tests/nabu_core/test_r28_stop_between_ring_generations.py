"""Stopp zwischen zwei Ring-Generationen (D4-Regression, R28).

Der Produktionspfad besteht aus kurzen Ring-Generationen (Datei zu Ende,
Restart-Pause von ``restart_delay``). Waehrend dieser Pause existiert im
Dispatcher kein ``_stop_signal`` mehr. Vor R28 endete ``SerialDispatcher.stop()``
dort mit einem blanken ``False``, ohne den Spieler je zu befragen: die Sitzung
meldete ``stop_unconfirmed``, das Journal ``stop_requested_unconfirmed`` - obwohl
das Ziel still war. Regressionsfaelle hierfuer:

* Stopp in der Restart-Pause (Pfad aus D/E/F),
* Stopp vor der ersten Generation (Sitzung aktiv, Interaktion laeuft),
* Stopp ohne laeuftige Generation bei fremder Wiedergabe (D5 bleibt geschuetzt),
* Stopp waehtrend einer lebenden Generation (Bestandspfad),
* stille Pause, in der der Spieler weiter spielt: ausdruecklich NICHT bestaetigt
  (kein Befehl, Journal bleibt ``stop_requested_unconfirmed``).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nabu_core.alarm_engine import AlarmStore
from nabu_core.alarm_session import AlarmSession
from nabu_core.devices import DevicePolicy
from nabu_core.dispatch import SerialDispatcher
from nabu_core.player import MediaPlayerAdapter
from nabu_core.scheduler import Scheduler

TARGET = "media_player.echo_2_c2709b_media_player"
FILE_SECONDS = 0.25
RESTART_DELAY = 0.4


class EchoLikeClient:
    """Medien-Datei endet nach ``file_seconds``; keine lesbare Medien-ID."""

    def __init__(self, file_seconds: float = FILE_SECONDS, state: str = "idle"):
        self.state = state
        self.volume = 0.3
        self.plays = 0
        self.stops = 0
        self.media_id = None
        self.file_seconds = file_seconds

    async def play_media(self, target, media):
        self.plays += 1
        self.state = "playing"
        if self.file_seconds:
            asyncio.get_running_loop().call_later(self.file_seconds, self._end)

    def _end(self):
        if self.state == "playing":
            self.state = "idle"

    async def stop(self, target):
        self.stops += 1
        self.state = "idle"

    async def get_state(self, target):
        return self.state

    async def get_media_id(self, target):
        return None

    async def get_volume(self, target):
        return self.volume

    async def set_volume(self, target, volume):
        self.volume = volume


def build(
    tmp_path: Path,
    *,
    ring_seconds: float = 4.0,
    interaction: bool = False,
    state: str = "idle",
    file_seconds: float = FILE_SECONDS,
):
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    store = AlarmStore(tmp_path / "alarms.sqlite")
    store.create(now, now=now - timedelta(seconds=1))
    client = EchoLikeClient(file_seconds=file_seconds, state=state)
    adapter = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}),
        media=("music", "alarm.wav"),
        max_seconds=ring_seconds + 10,
        volume=0.3,
        restart_delay=RESTART_DELAY,
    )
    dispatcher = SerialDispatcher(adapter, allowed_targets={TARGET}, max_seconds=ring_seconds + 10)

    def factory(target):
        return AlarmSession(
            ring=lambda: dispatcher.ring(TARGET),
            stop=dispatcher.stop,
            state_of=lambda: client.get_state(target),
            interaction_active=(lambda: asyncio.sleep(0, result=True)) if interaction else None,
            music=None,
            ring_seconds=ring_seconds,
            max_restarts=60,
            restart_delay=RESTART_DELAY,
        )

    scheduler = Scheduler(
        store, dispatcher, TARGET, session_factory=factory, session_max_seconds=ring_seconds
    )
    return now, scheduler, dispatcher, client


async def wait_until(predicate, budget: float = 8.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def test_stop_between_ring_generations_confirms(tmp_path):
    """D4: Stopp in der Restart-Pause muss als bestaetigt enden."""
    now, scheduler, dispatcher, client = build(tmp_path)
    tick = asyncio.create_task(scheduler.tick(now))

    # Generation laeuft gerade zu Ende: Signal weg, Spieler still, Sitzung aktiv.
    assert await wait_until(
        lambda: scheduler._active_session is not None
        and dispatcher._stop_signal is None
        and client.plays >= 1
        and client.state == "idle"
    ), "Restart-Pause wurde nicht erreicht"

    confirmed = await asyncio.wait_for(scheduler.stop(now), 10)
    plays_beim_stopp = client.plays
    await asyncio.wait_for(tick, 10)
    occurrence = scheduler.occurrences()[0]

    assert confirmed is True
    assert occurrence.detail == "stop_confirmed"

    # Nach stop_confirmed darf keine weitere Klingelgeneration beginnen:
    # Sitzung zu Ende, kein neues Signal, keine erneute Wiedergabe, Ziel still.
    await asyncio.sleep(2 * RESTART_DELAY + FILE_SECONDS + 0.5)
    assert client.plays == plays_beim_stopp, "nach dem Stopp laeuft eine neue Generation"
    assert dispatcher._stop_signal is None, "eine neue Generation haette das Signal gesetzt"
    assert scheduler._active_session is None, "die Sitzung ist nicht zu Ende"
    assert client.state == "idle", "das Ziel ist nicht still geblieben"
    assert scheduler.occurrences()[0].detail == "stop_confirmed"


async def test_silent_pause_is_not_confirmed_while_player_keeps_playing(tmp_path):
    """Stille Pause wird nur bei echter Stille bestaetigt - sonst kein Erfolg."""
    now, scheduler, dispatcher, client = build(tmp_path)
    tick = asyncio.create_task(scheduler.tick(now))

    assert await wait_until(
        lambda: scheduler._active_session is not None
        and dispatcher._stop_signal is None
        and client.plays >= 1
        and client.state == "idle"
    ), "Restart-Pause wurde nicht erreicht"

    # Die Pause ist nur der Uebergang zwischen zwei Generationen: spielt der
    # Spieler dort weiter, darf daraus kein bestaetigter Stopp werden.
    client.state = "playing"
    confirmed = await asyncio.wait_for(scheduler.stop(now), 10)
    await asyncio.wait_for(tick, 10)

    assert confirmed is False
    assert scheduler.occurrences()[0].detail == "stop_requested_unconfirmed"
    assert client.stops == 0, "ohne Stille-/Eigentumsnachweis wird kein Befehl gesendet"
    assert client.state == "playing"


async def test_stop_before_first_ring_generation_confirms(tmp_path):
    """Sitzung aktiv, noch nie geraeengt: Stille ist ehrlich bestaetigbar."""
    now, scheduler, dispatcher, client = build(tmp_path, interaction=True)
    tick = asyncio.create_task(scheduler.tick(now))

    assert await wait_until(lambda: scheduler._active_session is not None)
    assert client.plays == 0

    confirmed = await asyncio.wait_for(scheduler.stop(now), 10)
    await asyncio.wait_for(tick, 10)

    assert confirmed is True
    assert scheduler.occurrences()[0].detail == "stop_confirmed"


async def test_stop_without_ring_never_touches_unowned_playback(tmp_path):
    """D5: fremde Wiedergabe ohne Eigentumsnachweis bleibt unberuehrt."""
    client = EchoLikeClient(file_seconds=0.0, state="playing")
    adapter = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}),
        media=("music", "alarm.wav"),
        max_seconds=10.0,
        volume=0.3,
    )
    dispatcher = SerialDispatcher(adapter, allowed_targets={TARGET}, max_seconds=10.0)

    confirmed = await dispatcher.stop()

    assert confirmed is False
    assert client.stops == 0
    assert client.state == "playing"


async def test_stop_during_live_ring_generation_confirms(tmp_path):
    """Bestandspfad: Stopp waehrend einer laufenden Generation."""
    now, scheduler, dispatcher, client = build(tmp_path)
    tick = asyncio.create_task(scheduler.tick(now))

    assert await wait_until(
        lambda: dispatcher._stop_signal is not None and client.state == "playing"
    ), "laufende Generation wurde nicht erreicht"

    confirmed = await asyncio.wait_for(scheduler.stop(now), 10)
    await asyncio.wait_for(tick, 10)

    assert confirmed is True
    assert scheduler.occurrences()[0].detail == "stop_confirmed"
