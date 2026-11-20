"""A conversation must finish before an interrupted alarm can resume."""

import asyncio

from nabu_core.alarm_session import AlarmSession, SessionOutcome
from nabu_core.dispatch import DispatchResult


async def test_alarm_waits_for_interaction_to_finish_before_restarting():
    conversation = asyncio.Event()
    restarted = asyncio.Event()
    calls = 0

    async def ring():
        nonlocal calls
        calls += 1
        if calls == 1:
            return DispatchResult("completed")
        restarted.set()
        await asyncio.Event().wait()

    async def stop():
        return True

    async def state():
        return "idle"

    async def interaction_active():
        return calls > 0 and not conversation.is_set()

    session = AlarmSession(
        ring=ring,
        stop=stop,
        state_of=state,
        interaction_active=interaction_active,
        poll=0.01,
        ring_seconds=1,
    )
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.05)
    assert calls == 1
    conversation.set()
    await asyncio.wait_for(restarted.wait(), 0.5)
    await session.request_stop()
    report = await asyncio.wait_for(task, 0.5)
    assert report.outcome is SessionOutcome.STOPPED


async def test_unreadable_interaction_state_does_not_start_playback():
    calls = 0

    async def ring():
        nonlocal calls
        calls += 1

    async def stop():
        return True

    async def state():
        return "idle"

    async def unavailable():
        raise ConnectionError("offline")

    session = AlarmSession(ring=ring, stop=stop, state_of=state, interaction_active=unavailable)
    report = await session.run()
    assert report.outcome is SessionOutcome.NEEDS_ATTENTION
    assert report.detail == "interaction_state_unavailable:ConnectionError"
    assert calls == 0
