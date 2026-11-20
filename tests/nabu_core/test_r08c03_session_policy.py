"""Session policy: an unknown satellite must not silence the alarm, blocked retries.

R09 audit F-03: an unreadable satellite state ended the session with zero ring
attempts, so a short satellite outage made the alarm disappear. F-05: a busy or
unavailable player was given exactly one attempt. Both are fixed here, and both
fixes have to keep the old guarantees: a confirmed dialogue still pauses the ring,
and a permanently blocked target still asks for a human.
"""

import asyncio

import pytest

from nabu_core.alarm_session import AlarmSession, SessionOutcome
from nabu_core.dispatch import DispatchResult


def session(ring, *, interaction=None, player_state="idle", stop_fn=None, **kwargs):
    async def stop():
        if stop_fn is not None:
            return await stop_fn()
        return True

    async def state():
        return player_state() if callable(player_state) else player_state

    return AlarmSession(
        ring=ring,
        stop=stop,
        state_of=state,
        interaction_active=interaction,
        poll=0.01,
        **kwargs,
    )


async def test_unreadable_satellite_does_not_prevent_the_ring():
    calls = 0
    ringing = asyncio.Event()

    async def ring():
        nonlocal calls
        calls += 1
        ringing.set()
        await asyncio.Event().wait()

    async def unavailable():
        raise ConnectionError("offline")

    subject = session(ring, interaction=unavailable, ring_seconds=5)
    task = asyncio.create_task(subject.run())
    await asyncio.wait_for(ringing.wait(), 1)
    await subject.request_stop()
    report = await asyncio.wait_for(task, 2)
    assert calls == 1
    assert report.outcome is SessionOutcome.STOPPED
    assert "interaction_state_unavailable:ConnectionError" in report.warnings


async def test_non_boolean_satellite_state_does_not_prevent_the_ring():
    calls = 0
    ringing = asyncio.Event()

    async def ring():
        nonlocal calls
        calls += 1
        ringing.set()
        await asyncio.Event().wait()

    async def nowhere():
        return None

    subject = session(ring, interaction=nowhere, ring_seconds=5)
    task = asyncio.create_task(subject.run())
    await asyncio.wait_for(ringing.wait(), 1)
    await subject.request_stop()
    report = await asyncio.wait_for(task, 2)
    assert calls == 1
    assert report.outcome is SessionOutcome.STOPPED
    assert "interaction_state_unknown" in report.warnings


async def test_unknown_state_mid_ring_keeps_ringing():
    calls = 0
    ringing = asyncio.Event()
    unknown = asyncio.Event()

    async def ring():
        nonlocal calls
        calls += 1
        ringing.set()
        await asyncio.Event().wait()

    async def interaction():
        if unknown.is_set():
            raise TimeoutError("satellite entity went away")
        return False

    subject = session(ring, interaction=interaction, player_state="playing", ring_seconds=5)
    task = asyncio.create_task(subject.run())
    await asyncio.wait_for(ringing.wait(), 1)
    unknown.set()
    await asyncio.sleep(0.1)  # several supervision cycles with an unknown state
    assert calls == 1
    assert not task.done()
    await subject.request_stop()
    report = await asyncio.wait_for(task, 2)
    assert report.outcome is SessionOutcome.STOPPED


async def test_confirmed_dialogue_interrupts_the_ring_and_resumes_afterwards():
    count = 0
    stops = 0
    listening = asyncio.Event()

    async def ring():
        nonlocal count
        count += 1
        await asyncio.Event().wait()

    async def stop():
        nonlocal stops
        stops += 1
        return True

    async def interaction():
        return listening.is_set()

    subject = session(
        ring, interaction=interaction, player_state="playing", stop_fn=stop, ring_seconds=5
    )
    task = asyncio.create_task(subject.run())
    await asyncio.sleep(0.05)
    assert count == 1
    listening.set()
    await asyncio.sleep(0.15)
    # The dialogue stops the audio and holds the next ring back.
    assert stops >= 1
    assert count == 1
    listening.clear()
    await asyncio.sleep(0.1)
    assert count == 2
    await subject.request_stop()
    report = await asyncio.wait_for(task, 2)
    assert report.outcome is SessionOutcome.STOPPED


async def test_blocked_target_is_retried_before_giving_up():
    attempts = 0
    first_started = asyncio.Event()

    async def ring():
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return DispatchResult("blocked", "player_not_idle")
        first_started.set()
        await asyncio.Event().wait()

    subject = session(ring, player_state="playing", max_restarts=3, ring_seconds=5)
    task = asyncio.create_task(subject.run())
    await asyncio.wait_for(first_started.wait(), 2)
    assert attempts == 3
    await subject.request_stop()
    report = await asyncio.wait_for(task, 2)
    assert report.outcome is SessionOutcome.STOPPED
    assert report.restarts == 2


async def test_permanently_blocked_target_reports_the_block_reason():
    async def ring():
        return DispatchResult("blocked", "player_not_idle")

    subject = session(ring, max_restarts=2, ring_seconds=5)
    report = await asyncio.wait_for(subject.run(), 3)
    assert report.outcome is SessionOutcome.NEEDS_ATTENTION
    assert report.detail == "blocked:player_not_idle"
    assert report.restarts == 2


async def test_unavailable_player_is_retried_and_reported():
    async def ring():
        raise RuntimeError("alarm target is busy or unavailable")

    subject = session(ring, max_restarts=2, ring_seconds=5)
    report = await asyncio.wait_for(subject.run(), 3)
    assert report.outcome is SessionOutcome.NEEDS_ATTENTION
    assert "busy or unavailable" in report.detail
    assert report.restarts == 2


async def test_retry_budget_is_bounded_by_the_ring_deadline():
    attempts = 0

    async def ring():
        nonlocal attempts
        attempts += 1
        return DispatchResult("blocked", "player_not_idle")

    subject = session(
        ring, player_state="playing", max_restarts=1000, ring_seconds=0.3, restart_delay=0.02
    )
    report = await asyncio.wait_for(subject.run(), 5)
    assert report.outcome is SessionOutcome.NEEDS_ATTENTION
    assert attempts < 100
    assert report.restarts < 1000
    assert report.detail.startswith("blocked:")


@pytest.mark.parametrize("detail", ["harness", "mystery"])
async def test_unknown_outcomes_are_still_not_retried(detail):
    async def ring():
        return DispatchResult(detail, "whatever")

    subject = session(ring, max_restarts=5, ring_seconds=5)
    report = await asyncio.wait_for(subject.run(), 2)
    assert report.outcome is SessionOutcome.NEEDS_ATTENTION
    assert report.detail.startswith("unexpected_ring_result:")
    assert report.restarts == 0
