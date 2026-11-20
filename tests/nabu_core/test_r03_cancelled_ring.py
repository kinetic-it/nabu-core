"""A cancelled stream does not confirm that a logical alarm was stopped."""

import asyncio

from nabu_core.alarm_session import AlarmSession, SessionOutcome


async def test_cancelled_stream_is_not_confirmed_stop():
    async def ring():
        raise asyncio.CancelledError

    async def stop():
        return False

    async def state():
        return "idle"

    report = await AlarmSession(ring=ring, stop=stop, state_of=state).run()
    assert report.outcome is SessionOutcome.NEEDS_ATTENTION
