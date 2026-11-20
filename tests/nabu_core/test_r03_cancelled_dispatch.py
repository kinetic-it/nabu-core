"""Unknown or cancelled dispatch results cannot count as normal playback."""

from nabu_core.alarm_session import AlarmSession, SessionOutcome
from nabu_core.dispatch import DispatchResult


async def test_cancelled_dispatch_is_not_automatically_replayed():
    calls = 0

    async def ring():
        nonlocal calls
        calls += 1
        return DispatchResult("cancelled")

    async def stop():
        return True

    async def state():
        return "idle"

    report = await AlarmSession(ring=ring, stop=stop, state_of=state).run()
    assert report.outcome is SessionOutcome.NEEDS_ATTENTION
    assert calls == 1
