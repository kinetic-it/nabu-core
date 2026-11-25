"""R32.3: Ein haengender Sprach-Turn darf den Wecker nicht stumm schalten.

Hardware-Befund vom 25.11.2026, 19:05: Wakeword waehrend des Klingelns, danach
blieb der Satellit 35 s in `listening` (kein `processing`, keine Antwort). Der
Ring pausierte wegen `interaction_active` bis zum Ende des Ringfensters - der
Wecker war stumm, ohne gestoppt zu sein.

Die Ringgeneration verhaelt sich hier wie am Geraet: sie spielt eine Weile und
endet dann von selbst. Die Sitzung laeuft mit produktionsnahen Werten
(``max_restarts=60``, ``restart_delay``, ``poll``) - nur die Zeitskala ist
gestaucht, damit ein Testlauf nicht Minuten dauert.
"""

from __future__ import annotations

import asyncio

from nabu_core.alarm_session import AlarmSession, SessionOutcome

GRACE = 0.2
PLAY = 0.1
RESTART_DELAY = 0.05
POLL = 0.02


class GenerationEnded:
    """Was der Adapter nach einer vollstaendig gespielten Generation zurueckgibt."""

    status = "completed"
    detail = ""


class RealisticDevice:
    """Ringgenerationen wie am Echo: eine Weile Ton, dann Ende der Generation."""

    def __init__(self, play_seconds: float = PLAY) -> None:
        self.play_seconds = play_seconds
        self.starts: list[float] = []
        self.stop_times: list[float] = []

    async def ring(self) -> GenerationEnded:
        self.starts.append(asyncio.get_running_loop().time())
        await asyncio.sleep(self.play_seconds)
        return GenerationEnded()

    async def stop(self) -> bool:
        self.stop_times.append(asyncio.get_running_loop().time())
        return True

    async def state_of(self) -> str:
        return "playing"

    def gaps(self) -> list[float]:
        pairs = zip(self.starts, self.starts[1:], strict=False)
        return [later - earlier for earlier, later in pairs]


class Satellite:
    """Satellit mit Zeitplan: Folge von (aktiv, dauer) - der letzte Eintrag gilt weiter."""

    def __init__(self, *phases: tuple[bool, float], hang: bool = False) -> None:
        self.phases = phases or ((False, 0.0),)
        self.hang = hang
        self.started = asyncio.get_running_loop().time()

    def _active_at(self, elapsed: float) -> bool:
        if self.hang:
            return True
        cursor = 0.0
        for active, duration in self.phases:
            if elapsed < cursor + duration:
                return active
            cursor += duration
        return self.phases[-1][0]

    async def interaction_active(self) -> bool:
        return self._active_at(asyncio.get_running_loop().time() - self.started)


def build(
    *,
    grace: float = GRACE,
    ring_seconds: float = 1.0,
    satellite: Satellite,
    play_seconds: float = PLAY,
) -> tuple[RealisticDevice, AlarmSession]:
    device = RealisticDevice(play_seconds=play_seconds)
    session = AlarmSession(
        ring=device.ring,
        stop=device.stop,
        state_of=device.state_of,
        interaction_active=satellite.interaction_active,
        poll=POLL,
        max_restarts=60,
        ring_seconds=ring_seconds,
        restart_delay=RESTART_DELAY,
        interaction_grace=grace,
    )
    return device, session


async def test_a_hung_turn_gets_exactly_one_grace_window_then_the_alarm_keeps_ringing() -> None:
    satellite = Satellite(hang=True)
    device, session = build(ring_seconds=1.0, satellite=satellite)

    session_started = asyncio.get_running_loop().time()
    report = await session.run()

    assert report.outcome is SessionOutcome.RANG_OUT
    assert "interaction_hang" in report.warnings, report.warnings
    assert len(device.starts) >= 4, device.starts
    # Die Grace Period ist die Wartezeit vor der ersten Generation, nicht eine Luecke
    # zwischen zwei Generationen: danach klingelt es lueckenlos weiter.
    first_start = device.starts[0] - session_started
    assert first_start >= GRACE * 0.9, first_start
    gaps = device.gaps()
    # Kein zweites Fenster fuer denselben haengenden Turn.
    assert max(gaps) < GRACE * 0.9, gaps
    assert len(device.starts) <= 60, len(device.starts)


async def test_a_new_turn_after_a_hang_gets_its_own_grace_window() -> None:
    satellite = Satellite((True, 0.5), (False, 0.2), (True, 0.18), (False, 10.0))
    device, session = build(ring_seconds=1.2, satellite=satellite)

    report = await session.run()

    assert "interaction_hang" in report.warnings, report.warnings
    gaps = device.gaps()
    # Der neue, legitime Turn bekommt wieder eine Pause - der Latch ist zurueckgesetzt.
    assert sum(1 for gap in gaps if gap >= 0.25) == 1, gaps


async def test_a_short_turn_pauses_once_and_never_sets_the_hang_note() -> None:
    satellite = Satellite((True, 0.14), (False, 10.0))
    device, session = build(ring_seconds=1.0, satellite=satellite)

    session_started = asyncio.get_running_loop().time()
    report = await session.run()

    assert "interaction_hang" not in report.warnings, report.warnings
    first_start = device.starts[0] - session_started
    assert first_start >= 0.1, first_start
    gaps = device.gaps()
    assert max(gaps) < GRACE * 0.9, gaps


async def test_a_stop_during_a_hung_turn_wins_and_leaves_no_generation_behind() -> None:
    satellite = Satellite(hang=True)
    device, session = build(grace=5.0, ring_seconds=5.0, satellite=satellite)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.35)
    await session.request_stop()

    report = await asyncio.wait_for(task, 3.0)

    assert report.outcome is SessionOutcome.STOPPED
    assert device.stop_times, "kein Stopp an das Geraet"
    stopped_at = device.stop_times[0]
    assert all(start <= stopped_at for start in device.starts), device.starts


async def test_the_ring_window_is_never_extended_by_the_grace_period() -> None:
    satellite = Satellite(hang=True)
    device, session = build(grace=5.0, ring_seconds=0.35, satellite=satellite)

    report = await session.run()

    assert report.outcome is SessionOutcome.RANG_OUT
    assert "interaction_hang" not in report.warnings, report.warnings
    assert len(device.starts) <= 1, device.starts


async def test_an_unreadable_satellite_state_does_not_silence_the_alarm() -> None:
    async def broken() -> bool:
        raise RuntimeError("satellite state unavailable")

    device = RealisticDevice()
    session = AlarmSession(
        ring=device.ring,
        stop=device.stop,
        state_of=device.state_of,
        interaction_active=broken,
        poll=POLL,
        max_restarts=60,
        ring_seconds=0.6,
        restart_delay=RESTART_DELAY,
        interaction_grace=GRACE,
    )

    report = await session.run()

    assert report.outcome is SessionOutcome.RANG_OUT
    assert len(device.starts) >= 2, device.starts
    assert any("interaction_state_unavailable" in warning for warning in report.warnings)


def test_a_grace_of_zero_is_rejected() -> None:
    device = RealisticDevice()
    for value in (0.0, -1.0, 121.0):
        try:
            AlarmSession(
                ring=device.ring,
                stop=device.stop,
                state_of=device.state_of,
                interaction_grace=value,
            )
        except ValueError:
            continue
        raise AssertionError(f"interaction_grace={value} wurde akzeptiert")
