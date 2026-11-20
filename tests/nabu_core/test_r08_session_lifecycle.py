"""A completed stream is not a confirmed logical alarm stop."""

from nabu_core.alarm_session import AlarmSession, SessionOutcome


async def test_completed_stream_without_stop_is_not_reported_as_stopped():
    class Result:
        status = "completed"

    async def ring():
        return Result()

    async def stop():
        return True

    async def state():
        return "idle"

    session = AlarmSession(ring=ring, stop=stop, state_of=state, poll=0.01, max_restarts=0)
    report = await session.run()
    assert report.outcome is SessionOutcome.NEEDS_ATTENTION
