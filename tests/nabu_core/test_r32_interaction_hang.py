"""R32.3: Ein haengender Sprach-Turn darf den Wecker nicht stumm schalten.

Hardware-Befund vom 25.11.2026, 19:05: Wakeword waehrend des Klingelns, danach
blieb der Satellit 35 s in `listening` (kein `processing`, keine Antwort). Der
Ring pausierte wegen `interaction_active` bis zum Ende des Ringfensters - der
Wecker war stumm, ohne gestoppt zu sein. Ein Turn, der die Pipeline nie
verlaesst, ist kein Turn: nach `interaction_grace` muss weitergeklingelt werden.
"""

from __future__ import annotations

import asyncio

from nabu_core.alarm_session import AlarmSession, SessionOutcome


class Completion:
    status = "completed"
    detail = ""


class FakeDevice:
    def __init__(self) -> None:
        self.plays = 0
        self.stops = 0

    async def ring(self) -> Completion:
        self.plays += 1
        return Completion()

    async def stop(self) -> bool:
        self.stops += 1
        return True

    async def state_of(self) -> str:
        return "idle"


class Satellite:
    """Satelliten-State als steuerbare Quelle; `hang` bleibt dauerhaft aktiv."""

    def __init__(self, *, hang: bool = False, busy_for: float = 0.0) -> None:
        self.hang = hang
        self.busy_for = busy_for
        self.started = asyncio.get_running_loop().time()
        self.calls = 0

    async def interaction_active(self) -> bool:
        self.calls += 1
        if self.hang:
            return True
        return asyncio.get_running_loop().time() - self.started < self.busy_for


def build(
    *, grace: float, ring_seconds: float = 0.8, satellite: Satellite
) -> tuple[FakeDevice, AlarmSession]:
    device = FakeDevice()
    session = AlarmSession(
        ring=device.ring,
        stop=device.stop,
        state_of=device.state_of,
        interaction_active=satellite.interaction_active,
        poll=0.01,
        max_restarts=1000,
        ring_seconds=ring_seconds,
        restart_delay=0.0,
        interaction_grace=grace,
    )
    return device, session


async def test_a_hung_voice_turn_does_not_silence_the_alarm() -> None:
    satellite = Satellite(hang=True)
    device, session = build(grace=0.2, ring_seconds=0.9, satellite=satellite)

    report = await session.run()

    assert report.outcome is SessionOutcome.RANG_OUT
    assert device.plays >= 3, device.plays
    assert "interaction_hang" in report.warnings, report.warnings


async def test_a_normal_voice_turn_pauses_the_alarm_without_a_hang_note() -> None:
    satellite = Satellite(busy_for=0.15)
    device, session = build(grace=0.5, ring_seconds=0.9, satellite=satellite)

    report = await session.run()

    assert device.plays >= 1, device.plays
    assert "interaction_hang" not in report.warnings, report.warnings


async def test_the_grace_never_extends_the_ring_window() -> None:
    satellite = Satellite(hang=True)
    device, session = build(grace=5.0, ring_seconds=0.3, satellite=satellite)

    report = await session.run()

    assert report.outcome is SessionOutcome.RANG_OUT
    assert device.plays == 0, device.plays
    assert "interaction_hang" not in report.warnings, report.warnings


async def test_a_stop_during_a_hung_turn_still_stops() -> None:
    satellite = Satellite(hang=True)
    device, session = build(grace=5.0, ring_seconds=5.0, satellite=satellite)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.1)
    await session.request_stop()

    report = await asyncio.wait_for(task, 3.0)

    assert report.outcome is SessionOutcome.STOPPED
    assert device.plays == 0, device.plays


def test_a_grace_of_zero_is_rejected() -> None:
    device = FakeDevice()
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
