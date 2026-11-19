"""Alarm session: the sequence around one ring, not the ring itself."""

import asyncio
from dataclasses import dataclass

import pytest

from nabu_core.alarm_session import AlarmSession, SessionOutcome


@dataclass
class RingResult:
    outcome: str
    detail: str = ""


class Device:
    """A player plus the dispatcher's verdict, in one small fake."""

    def __init__(self, *, state="playing", stop_confirms=True, fail_ring=None, ring_block=True):
        self.state = state
        self.stop_confirms = stop_confirms
        self.fail_ring = fail_ring
        self.ring_block = ring_block
        self.stops = 0
        self.rings = 0
        self.released = asyncio.Event()

    async def ring(self):
        self.rings += 1
        if self.fail_ring:
            raise self.fail_ring
        if self.ring_block:
            self.released = asyncio.Event()     # each ring waits for its own stop
            await self.released.wait()
        return RingResult("stopped", "physical_silence_unconfirmed")

    async def stop(self):
        self.stops += 1
        if self.stop_confirms:
            self.state = "idle"
            self.released.set()                 # a refused stop must not end the ring
        return self.stop_confirms

    async def state_of(self):
        return self.state


class Music:
    def __init__(self, offers=True, fail_resume=False):
        self.offers = offers
        self.fail_resume = fail_resume
        self.pauses = 0
        self.resumes = 0

    async def pause(self):
        self.pauses += 1
        return "token" if self.offers else None

    async def resume(self, token):
        assert token == "token"
        self.resumes += 1
        if self.fail_resume:
            raise OSError("resume refused")
        return True


def session(device, music=None, **kwargs):
    return AlarmSession(
        ring=device.ring, stop=device.stop, state_of=device.state_of, music=music, **kwargs
    )


async def run_with_stop(sess, device, after=0.05):
    task = asyncio.create_task(sess.run())
    await asyncio.sleep(after)
    await sess.request_stop()
    return await asyncio.wait_for(task, 2)


async def test_happy_path_stops_and_gives_the_music_back():
    device, music = Device(), Music()
    report = await run_with_stop(session(device, music), device)
    assert report.outcome is SessionOutcome.STOPPED
    assert device.rings == 1 and device.stops == 1
    assert music.pauses == 1 and music.resumes == 1
    assert report.restarts == 0


async def test_music_is_paused_before_the_first_ring():
    order = []
    device, music = Device(), Music()
    device_ring = device.ring

    async def ring():
        order.append("ring")
        return await device_ring()

    async def pause():
        order.append("pause")
        return "token"

    music.pause = pause
    sess = AlarmSession(ring=ring, stop=device.stop, state_of=device.state_of, music=music)
    await run_with_stop(sess, device)
    assert order[:2] == ["pause", "ring"]


async def test_a_file_that_ends_early_does_not_end_the_alarm():
    device = Device(ring_block=True)            # the ring call is still pending ...
    sess = session(device, Music(), poll=0.01, max_restarts=2)
    task = asyncio.create_task(sess.run())
    await asyncio.sleep(0.02)
    device.state = "idle"                       # ... but the player dropped the media
    report = await asyncio.wait_for(task, 3)
    assert report.outcome is SessionOutcome.NEEDS_ATTENTION
    assert report.restarts == 2                 # bounded, not an endless retry
    assert device.rings == 3
    assert "player_idle" in report.detail


async def test_restart_after_a_dropped_playback_keeps_ringing():
    device = Device(ring_block=False)
    device.state = "playing"
    sess = session(device, Music(), poll=0.01)
    task = asyncio.create_task(sess.run())
    await asyncio.sleep(0.12)
    await sess.request_stop()
    report = await asyncio.wait_for(task, 2)
    assert report.outcome is SessionOutcome.STOPPED
    assert device.rings >= 1


async def test_failed_start_leaves_no_alarm_and_restores_music():
    device, music = Device(fail_ring=RuntimeError("play_media failed")), Music()
    report = await asyncio.wait_for(session(device, music).run(), 2)
    assert report.outcome is SessionOutcome.START_FAILED
    assert device.stops == 1
    assert music.resumes == 1


async def test_unconfirmed_stop_keeps_the_music_paused():
    device, music = Device(stop_confirms=False), Music()
    report = await run_with_stop(session(device, music), device)
    assert report.outcome is SessionOutcome.STOP_UNCONFIRMED
    assert music.resumes == 0                   # never two streams at once
    assert device.stops == 1


async def test_ring_limit_ends_the_session_without_a_second_alarm():
    device = Device(ring_block=True)
    sess = session(device, Music(), poll=0.01, ring_seconds=0.05)
    report = await asyncio.wait_for(sess.run(), 2)
    assert report.outcome is SessionOutcome.RANG_OUT
    assert device.stops == 1


async def test_no_music_playing_means_no_pause_and_no_resume():
    device, music = Device(), Music(offers=False)
    report = await run_with_stop(session(device, music), device)
    assert report.outcome is SessionOutcome.STOPPED
    assert music.pauses == 1 and music.resumes == 0


async def test_music_resume_failure_is_reported_not_raised():
    device, music = Device(), Music(fail_resume=True)
    report = await run_with_stop(session(device, music), device)
    assert report.outcome is SessionOutcome.STOPPED
    assert music.resumes == 1


def test_rejects_nonsense_settings():
    device = Device()
    with pytest.raises(ValueError):
        session(device, poll=0)
    with pytest.raises(ValueError):
        session(device, max_restarts=-1)
    with pytest.raises(ValueError):
        session(device, ring_seconds=0)
