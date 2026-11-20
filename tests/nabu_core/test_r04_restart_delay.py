"""Delayed interaction events are given a bounded settling window."""

import asyncio

from nabu_core.alarm_session import AlarmSession
from nabu_core.dispatch import DispatchResult


async def test_stop_during_restart_delay_prevents_replay():
    first = asyncio.Event()
    count = 0

    async def ring():
        nonlocal count
        count += 1
        first.set()
        return DispatchResult("completed")

    async def stop():
        return True

    async def state():
        return "idle"

    async def interaction():
        return False

    session = AlarmSession(
        ring=ring,
        stop=stop,
        state_of=state,
        interaction_active=interaction,
        poll=0.01,
        ring_seconds=1,
        restart_delay=0.2,
    )
    task = asyncio.create_task(session.run())
    await first.wait()
    await asyncio.sleep(0.03)
    assert count == 1
    await session.request_stop()
    await asyncio.wait_for(task, 0.5)
    assert count == 1
