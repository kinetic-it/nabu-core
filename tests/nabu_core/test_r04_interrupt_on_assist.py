"""Assist begins even when a media player has not stopped itself."""

import asyncio

from nabu_core.alarm_session import AlarmSession
from nabu_core.dispatch import DispatchResult


async def test_active_listening_interrupts_playback_and_waits(tmp_path):
    listening = asyncio.Event()
    first_started = asyncio.Event()
    stopped = asyncio.Event()
    allow_resume = asyncio.Event()
    count = 0

    async def ring():
        nonlocal count
        count += 1
        first_started.set()
        await stopped.wait()
        return DispatchResult("stopped")

    async def stop():
        stopped.set()
        return True

    async def state():
        return "idle" if stopped.is_set() else "playing"

    async def interaction():
        return listening.is_set() and not allow_resume.is_set()

    session = AlarmSession(
        ring=ring,
        stop=stop,
        state_of=state,
        interaction_active=interaction,
        poll=0.01,
        ring_seconds=2,
    )
    task = asyncio.create_task(session.run())
    await first_started.wait()
    listening.set()
    await asyncio.wait_for(stopped.wait(), 0.3)
    await asyncio.sleep(0.05)
    assert count == 1
    await session.request_stop()
    await asyncio.wait_for(task, 1)
    assert count == 1
