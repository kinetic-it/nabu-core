"""Unexpected playback interruptions must not kill a ringing alarm silently.

Before R08.4 the ring loop treated *any* state that is neither playing nor idle as
fatal: one ``unavailable`` or ``unknown`` poll — a Wi-Fi hiccup, an Alexa hiccup,
a mutated entity — ended the ring with a bare ``RuntimeError``. The alarm went
silent and the journal only carried a class name.

Now: a dropped poll is logged and recovered within a bounded budget, and only a
player that stays away ends the ring — with an explicit error and a stop attempt.
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
    adapter._start_timeout = 0.15
    return adapter


class DroppingPlayer:
    """Plays normally, but becomes unreachable inside a window of the ring.

    ``dropout_from``/``dropout_to`` are seconds after the first accepted play.
    ``persistent=True`` keeps the player away until ``stop()`` is called; a
    transient dropout (the realistic Wi-Fi hiccup) clears itself.
    """

    def __init__(
        self,
        *,
        duration: float = 5.0,
        dropout_from: float | None = None,
        dropout_to: float | None = None,
        dropout_state: str = "unavailable",
        persistent: bool = False,
        raise_at: float | None = None,
    ):
        self.duration = duration
        self.dropout_from = dropout_from
        self.dropout_to = dropout_to
        self.dropout_state = dropout_state
        self.persistent = persistent
        self.raise_at = raise_at
        self.plays = 0
        self.stops = 0
        self.started_at: float | None = None
        self.armed_at: float | None = None
        self.raised = False
        self.volume = 0.5

    async def play_media(self, entity_id, media):
        self.plays += 1
        self.started_at = asyncio.get_running_loop().time()
        if self.plays == 1:
            self.armed_at = self.started_at

    async def stop(self, entity_id):
        self.stops += 1
        self.started_at = None
        self.armed_at = None  # a stop implies the player is reachable again

    async def get_state(self, entity_id):
        now = asyncio.get_running_loop().time()
        if self.armed_at is not None and self.raise_at is not None and not self.raised:
            if now - self.armed_at >= self.raise_at:
                self.raised = True
                raise TimeoutError("state read timed out")
        if self.armed_at is not None and self.dropout_from is not None:
            if now - self.armed_at >= self.dropout_from:
                limit = self.dropout_to
                if self.persistent or limit is None or now - self.armed_at < limit:
                    return self.dropout_state
        if self.started_at is None:
            return "idle"
        if now - self.started_at >= self.duration:
            return "idle"
        return "playing"

    async def get_volume(self, entity_id):
        return self.volume

    async def set_volume(self, entity_id, level):
        self.volume = float(level)


async def test_one_unavailable_poll_is_recovered(caplog: pytest.LogCaptureFixture):
    player = DroppingPlayer(dropout_from=0.3, dropout_to=0.6, duration=5.0)
    adapter = make_adapter(player, max_seconds=1.5, repeat=True, restart_delay=0.02)

    outcome = await adapter.play(TARGET)

    assert outcome.detail
    assert player.plays >= 2, "the ring is restarted after the drop"
    assert player.stops == 1, "exactly one cleanup"
    messages = [record.getMessage() for record in caplog.records]
    assert any("Alarm playback interruption 1/5" in message for message in messages)


async def test_unreadable_state_is_recovered_instead_of_crashing(
    caplog: pytest.LogCaptureFixture,
):
    player = DroppingPlayer(duration=5.0, raise_at=0.3)
    adapter = make_adapter(player, max_seconds=1.5, repeat=True, restart_delay=0.02)

    await adapter.play(TARGET)

    assert player.plays >= 2
    assert player.stops == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any("Alarm state unreadable" in message for message in messages)


async def test_paused_by_somebody_else_counts_as_interruption():
    player = DroppingPlayer(dropout_from=0.3, dropout_to=0.6, dropout_state="paused")
    adapter = make_adapter(player, max_seconds=1.5, repeat=True, restart_delay=0.02)

    await adapter.play(TARGET)

    assert player.plays >= 2
    assert player.stops == 1


async def test_permanent_dropout_ends_the_ring_honestly_and_restores_volume():
    player = DroppingPlayer(dropout_from=0.3, persistent=True, duration=60.0)
    adapter = make_adapter(player, max_seconds=2.0, repeat=True, restart_delay=0.02, volume=0.3)

    with pytest.raises(RuntimeError):
        await adapter.play(TARGET)

    assert player.stops == 1, "a failed ring still stops the player"
    assert player.volume == pytest.approx(0.5), "the previous volume is restored"


async def test_unreachable_player_is_not_claimed_to_be_restored():
    # Honest failure: if the player cannot be reached for the stop, the volume must
    # not be raised blindly (that would make a foreign stream loud).
    player = DroppingPlayer(dropout_from=0.3, persistent=True, duration=60.0)

    async def refuse_stop(entity_id):
        raise TimeoutError("player unreachable")

    player.stop = refuse_stop  # type: ignore[method-assign]
    adapter = make_adapter(player, max_seconds=2.0, repeat=True, restart_delay=0.02, volume=0.3)

    with pytest.raises(RuntimeError):
        await adapter.play(TARGET)

    assert player.volume == pytest.approx(0.3), "volume stays untouched while unconfirmed"
