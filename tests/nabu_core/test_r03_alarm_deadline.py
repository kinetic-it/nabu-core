"""The alarm timeout is global, not reset for each playback attempt."""

import asyncio

from nabu_core.alarm_session import AlarmSession, SessionOutcome


async def test_restarts_do_not_extend_the_logical_alarm_deadline():
    class Result:
        status = "completed"

    calls = 0

    async def ring():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.03)
        return Result()

    async def stop():
        return True

    async def state():
        return "playing"

    session = AlarmSession(
        ring=ring, stop=stop, state_of=state, ring_seconds=0.05, poll=0.005, max_restarts=5
    )
    report = await asyncio.wait_for(session.run(), 1)
    assert report.outcome is SessionOutcome.RANG_OUT
    assert calls < 5
