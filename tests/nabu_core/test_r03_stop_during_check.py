"""A stop during the interaction check fences any new playback."""

import asyncio

from nabu_core.alarm_session import AlarmSession


async def test_stop_during_interaction_check_prevents_start():
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def ring():
        nonlocal calls
        calls += 1

    async def stop():
        return True

    async def state():
        return "idle"

    async def interaction():
        entered.set()
        await release.wait()
        return False

    session = AlarmSession(ring=ring, stop=stop, state_of=state, interaction_active=interaction)
    task = asyncio.create_task(session.run())
    await entered.wait()
    await session.request_stop()
    release.set()
    await asyncio.wait_for(task, 1)
    assert calls == 0
