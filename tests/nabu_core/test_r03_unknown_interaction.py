"""An unknown conversation state must never be treated as idle."""

from nabu_core.alarm_session import AlarmSession, SessionOutcome


async def test_unknown_interaction_state_blocks_alarm_start():
    calls = 0

    async def ring():
        nonlocal calls
        calls += 1

    async def stop():
        return True

    async def state():
        return "idle"

    async def interaction_active():
        return None

    session = AlarmSession(
        ring=ring, stop=stop, state_of=state, interaction_active=interaction_active
    )
    report = await session.run()
    assert report.outcome is SessionOutcome.NEEDS_ATTENTION
    assert calls == 0
