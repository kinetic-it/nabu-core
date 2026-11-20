"""An unknown conversation state must not silence the alarm.

R08.1 treated a non-boolean satellite state as a contradiction and refused to ring
at all (R09 F-03: zero ring attempts). Unknown is not evidence of a dialogue, so the
alarm rings and the degradation is reported in the session warnings.
"""

import asyncio

from nabu_core.alarm_session import AlarmSession, SessionOutcome


async def test_unknown_interaction_state_does_not_block_alarm_start():
    calls = 0
    started = asyncio.Event()

    async def ring():
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.Event().wait()

    async def stop():
        return True

    async def state():
        return "idle"

    async def interaction_active():
        return None

    session = AlarmSession(
        ring=ring,
        stop=stop,
        state_of=state,
        interaction_active=interaction_active,
        poll=0.01,
        ring_seconds=5,
    )
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(started.wait(), 1)
    await session.request_stop()
    report = await asyncio.wait_for(task, 2)
    assert calls == 1
    assert report.outcome is SessionOutcome.STOPPED
    assert "interaction_state_unknown" in report.warnings
