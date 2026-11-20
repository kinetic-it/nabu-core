"""Playback confirmation: a slow start must not abort a running ring.

Field finding (2026-11-20, Echo 2 on HA 2026.11.2): one of six rings ended as
``failed: RuntimeError`` about ten seconds after the start. Home Assistant's own
history showed the player reporting ``playing`` after the adapter had already
given up, so the media call had been accepted — the player was simply slower than
the confirmation window, and the ring loop treated that as fatal.

The rule these tests pin down:

* a rejected media call (``play_media`` raises) stays fatal — nothing was started;
* a *not yet confirmed* start inside a running ring is retried with a settle pause
  and a small budget, and is never the reason to end an alarm that is already
  ringing;
* exhausted restarts end the ring with a clear error instead of a silent dead alarm;
* a long media file never needs a restart at all, so the short-file cadence (and
  its audible gaps) disappears.
"""

import asyncio

import pytest

from nabu_core.devices import DevicePolicy
from nabu_core.player import MediaPlayerAdapter

TARGET = "media_player.lab"
ALARM = "media-source://media_source/local/alarm.wav"


def make_adapter(client, **kwargs) -> MediaPlayerAdapter:
    adapter = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={TARGET}, denied=set()),
        media=("audio/wav", ALARM),
        **kwargs,
    )
    # White-box: the confirmation window is derived from max_seconds, which would
    # make every one of these tests take five seconds per attempt.
    adapter._start_timeout = 0.15
    return adapter


class TimedPlayer:
    """Minimal player: reports playing for ``duration`` after accepting a play.

    ``latency_schedule`` models a player that answers the first call quickly and
    later calls slowly — exactly the pattern that broke the ring.
    """

    def __init__(self, *, duration: float = 5.0, latency: float = 0.0, latency_schedule=None):
        self.duration = duration
        self.latency = latency
        self.latency_schedule = latency_schedule or []
        self.plays = 0
        self.stops = 0
        self.started_at: float | None = None
        self.volume = 0.5

    def _latency(self) -> float:
        if not self.latency_schedule:
            return self.latency
        return self.latency_schedule[min(self.plays - 1, len(self.latency_schedule) - 1)]

    async def play_media(self, entity_id, media):
        self.plays += 1
        self.started_at = asyncio.get_running_loop().time() + self._latency()

    async def stop(self, entity_id):
        self.stops += 1
        self.started_at = None

    async def get_state(self, entity_id):
        if self.started_at is None:
            return "idle"
        now = asyncio.get_running_loop().time()
        if now < self.started_at:
            return "idle"  # accepted but not audible yet
        if now - self.started_at >= self.duration:
            return "idle"  # media finished
        return "playing"

    async def get_volume(self, entity_id):
        return self.volume

    async def set_volume(self, entity_id, level):
        self.volume = float(level)


async def test_slow_restart_is_retried_instead_of_aborting_the_ring():
    # First start is fast, the restart after the 0.2 s media needs 0.4 s: longer
    # than the 0.15 s confirmation window, short enough to succeed on the retry.
    player = TimedPlayer(duration=0.2, latency_schedule=[0.0, 0.4, 0.0])
    adapter = make_adapter(player, max_seconds=1.5, repeat=True, restart_delay=0.05)

    outcome = await adapter.play(TARGET)

    assert player.plays >= 3, "the unconfirmed restart must be re-issued"
    assert player.stops == 1
    assert outcome.detail


async def test_restarts_are_bounded_and_end_with_a_clear_error():
    # Every restart after the first one stays unconfirmed: the budget must run out
    # instead of hammering the player until the deadline.
    player = TimedPlayer(duration=0.2, latency_schedule=[0.0, 5.0])
    adapter = make_adapter(player, max_seconds=2.0, repeat=True, restart_delay=0.01, max_restarts=2)

    with pytest.raises(RuntimeError, match="restarts exhausted"):
        await adapter.play(TARGET)

    assert player.plays == 3, "first start plus two restart attempts"
    # The media had already run out, so cleanup confirms "idle" without a stop call.
    assert player.stops == 0


async def test_rejected_media_call_is_still_fatal_without_retries():
    class Rejecting(TimedPlayer):
        async def play_media(self, entity_id, media):
            self.plays += 1
            raise OSError("player refused the media")

    player = Rejecting()
    adapter = make_adapter(player, max_seconds=1.0, repeat=True, restart_delay=0.01)

    with pytest.raises(RuntimeError, match="play_media failed"):
        await adapter.play(TARGET)
    assert player.plays == 1, "a rejected call is never retried"


async def test_long_media_needs_no_restart_and_keeps_ringing():
    # A 60 s alarm file: one start, one playing phase, no restart, no gaps.
    player = TimedPlayer(duration=60.0)
    adapter = make_adapter(player, max_seconds=0.6, repeat=True, restart_delay=0.05)

    outcome = await adapter.play(TARGET)

    assert player.plays == 1
    assert player.stops == 1
    assert outcome.detail


async def test_short_media_restarts_after_the_settle_pause():
    # 0.2 s media and a 0.05 s settle pause: the ring must keep restarting, but
    # only after the pause, never immediately on the idle observation.
    player = TimedPlayer(duration=0.2)
    adapter = make_adapter(player, max_seconds=0.7, repeat=True, restart_delay=0.05)

    await adapter.play(TARGET)

    assert player.plays >= 2, "the short file is restarted after the settle pause"
