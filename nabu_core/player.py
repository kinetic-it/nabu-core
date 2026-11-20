"""Media adapter that separates command acceptance from audible playback.

A positive ``play_media`` return only proves that the service call was
accepted. Audible playback starts only when the entity reports a playing
state, and a stop is only "confirmed" when the entity stops reporting it.
Nothing here may claim physical silence.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .devices import DevicePolicy
from .volume_journal import VolumeJournal

_LOGGER = logging.getLogger(__name__)
IO_TIMEOUT = 1.0
VOLUME_TOLERANCE = 0.005
STOPPED_STATES = frozenset({"idle", "off", "paused", "standby"})
PLAYING_STATES = frozenset({"playing", "buffering"})


class PlaybackStatus(StrEnum):
    """Explicit, non-overlapping outcomes for one playback step."""

    ACCEPTED = "accepted"
    PLAYING_CONFIRMED = "playing_confirmed"
    START_UNCONFIRMED = "start_unconfirmed"
    STOPPED_CONFIRMED = "stopped_confirmed"
    STOP_UNCONFIRMED = "stop_unconfirmed"
    FAILED = "failed"
    DUCK_CONFIRMED = "duck_confirmed"
    DUCK_UNCONFIRMED = "duck_unconfirmed"
    DUCK_FAILED = "duck_failed"


@dataclass(frozen=True, slots=True)
class PlaybackOutcome:
    status: PlaybackStatus
    entity_id: str
    detail: str = ""


class MediaClient(Protocol):
    """Minimal Home-Assistant-shaped media interface; injected, never imported."""

    async def play_media(self, entity_id: str, media: tuple[str, str]) -> None: ...

    async def stop(self, entity_id: str) -> None: ...

    async def get_state(self, entity_id: str) -> str: ...


@dataclass
class _Ring:
    target: str
    generation: str | None = None
    baseline: float | None = None
    written: float | None = None
    started: bool = False
    play_attempted: bool = False
    stopping: bool = False
    displaced: bool = False
    volume_released: bool = False
    attempted: float | None = None
    media_id: str | None = None
    duck_until: float = 0.0
    duck_active: bool = False
    cleanup: asyncio.Task[PlaybackOutcome] | None = None


class MediaPlayerAdapter:
    """Gate every call through the device policy and report honest states."""

    def __init__(
        self,
        client: MediaClient,
        policy: DevicePolicy,
        *,
        media: tuple[str, str],
        max_seconds: float = 600.0,
        repeat: bool = False,
        volume: float | None = None,
        cycle_on: float = 0.0,
        cycle_off: float = 0.0,
        recovery_store=None,
        allow_paused: bool = False,
    ) -> None:
        if not 0 < max_seconds <= 3600:
            raise ValueError("max_seconds must be positive and at most 3600")
        self._client = client
        self._policy = policy
        self._media = media
        # A paused player is occupied by somebody's queue. Only a session that
        # owns that pause (the music controller) may start on top of it.
        self._allow_paused = allow_paused
        self._max_seconds = max_seconds
        self._start_timeout = min(max_seconds, 5.0)
        self._stop_timeout = min(max_seconds, 5.0)
        self._stop_requested = asyncio.Event()
        if volume is not None and not 0 <= volume <= 1:
            raise ValueError("volume must be between 0 and 1")
        if not all(math.isfinite(v) and v >= 0 for v in (cycle_on, cycle_off)):
            raise ValueError("ring cycle must be finite and non-negative")
        if cycle_off and (not cycle_on or volume is None):
            raise ValueError("silent ring cycle requires an on duration and ring volume")
        self._repeat = repeat
        self._volume = volume
        self._cycle_on = cycle_on
        self._cycle_off = cycle_off
        self._media_lock = asyncio.Lock()
        self._ring: _Ring | None = None
        self._pending_restore: dict[str, _Ring] = {}
        self._journal = VolumeJournal(recovery_store) if recovery_store is not None else None

    @property
    def policy(self) -> DevicePolicy:
        return self._policy

    async def start(self, entity_id: str) -> PlaybackOutcome:
        """Request playback. Success means accepted, never audible."""
        target = self._policy.check(entity_id)
        try:
            async with asyncio.timeout(IO_TIMEOUT):
                await self._client.play_media(target, self._media)
        except Exception as error:
            return PlaybackOutcome(PlaybackStatus.FAILED, target, type(error).__name__)
        return PlaybackOutcome(PlaybackStatus.ACCEPTED, target, "command_accepted")

    async def confirm_playing(
        self, entity_id: str, *, wait: float = 5.0, interval: float = 0.2
    ) -> PlaybackOutcome:
        """Poll until the entity reports playback, without inventing success."""
        target = self._policy.check(entity_id)
        deadline = asyncio.get_running_loop().time() + max(wait, 0.0)
        last_error = ""
        while True:
            try:
                state = await self._state(target)
            except Exception as error:  # unreadable state is not a success
                last_error = type(error).__name__
            else:
                if str(state).lower() in PLAYING_STATES:
                    return PlaybackOutcome(PlaybackStatus.PLAYING_CONFIRMED, target, str(state))
                last_error = f"state={state}"
            if asyncio.get_running_loop().time() >= deadline:
                return PlaybackOutcome(PlaybackStatus.START_UNCONFIRMED, target, last_error)
            await asyncio.sleep(min(interval, max(wait, 0.0)))

    async def stop(
        self, entity_id: str, *, wait: float = 5.0, interval: float = 0.2
    ) -> PlaybackOutcome:
        """Request a stop and report whether the entity stopped reporting playback."""
        target = self._policy.check(entity_id)
        ring = self._ring
        if ring is not None and ring.target == target and ring.cleanup is None:
            return await self.stop_ring(target)
        if ring is not None and ring.target == target:
            return await asyncio.shield(ring.cleanup)
        return await self._stop_transport(target, wait=wait, interval=interval)

    async def _stop_transport(
        self, target: str, *, wait: float, interval: float
    ) -> PlaybackOutcome:
        try:
            async with asyncio.timeout(IO_TIMEOUT):
                await self._client.stop(target)
        except Exception as error:
            return PlaybackOutcome(PlaybackStatus.FAILED, target, type(error).__name__)
        deadline = asyncio.get_running_loop().time() + max(wait, 0.0)
        detail = "stop_requested"
        while True:
            try:
                state = await self._state(target)
            except Exception as error:
                detail = f"state_unreadable:{type(error).__name__}"
            else:
                detail = f"state={state}"
                if state in STOPPED_STATES:
                    return PlaybackOutcome(PlaybackStatus.STOPPED_CONFIRMED, target, detail)
            if asyncio.get_running_loop().time() >= deadline:
                return PlaybackOutcome(PlaybackStatus.STOP_UNCONFIRMED, target, detail)
            await asyncio.sleep(min(interval, max(wait, 0.0)))

    async def _state(self, target: str) -> str:
        async with asyncio.timeout(IO_TIMEOUT):
            return str(await self._client.get_state(target)).lower()

    async def _read_volume(self, target: str) -> float | None:
        reader = getattr(self._client, "get_volume", None)
        if reader is None:
            raise RuntimeError("volume read is not supported")
        async with asyncio.timeout(IO_TIMEOUT):
            value = await reader(target)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
            return float(value)
        return None

    @staticmethod
    def _matches(current: float | None, expected: float) -> bool:
        # Do not count an already quiet but nonzero speaker as a verified mute.
        return current is not None and abs(current - expected) <= (
            0.0 if expected == 0 else VOLUME_TOLERANCE
        )

    async def _identify_media(self, ring: _Ring) -> bool | None:
        """Does the player still report our alarm? None when it cannot say.

        A player that exposes no media identifier leaves ownership unproven. That is
        not the same as "it is ours": stopping on a guess can silence somebody else's
        music, so callers must treat None as a refusal, not as consent.
        """
        reader = getattr(self._client, "get_media_id", None)
        if reader is None:
            return None
        async with asyncio.timeout(IO_TIMEOUT):
            observed = await reader(ring.target)
        if not observed:
            return None
        expected = ring.media_id or self._media[1]
        if observed != expected:
            ring.displaced = True
        elif ring.started:
            ring.media_id = observed
        return not ring.displaced

    async def _set_volume(self, ring: _Ring, level: float) -> bool:
        setter = getattr(self._client, "set_volume", None)
        if setter is None:
            raise RuntimeError("volume write is not supported")
        current = await self._read_volume(ring.target)
        if current is None:
            raise RuntimeError("player volume is not observable")
        if ring.baseline is None:
            ring.baseline = current
        expected = ring.baseline if ring.written is None else ring.written
        if ring.volume_released or not (
            self._matches(current, expected)
            or (ring.attempted is not None and self._matches(current, ring.attempted))
        ):
            ring.volume_released = True
            return False
        if self._matches(current, level):
            ring.written, ring.attempted = level, None
            return True
        # Record the attempted level before awaiting: cancellation or a transport
        # error may occur after the player already applied the value.
        if self._journal is not None:
            if ring.generation is None:
                ring.generation = self._journal.begin(ring.target, ring.baseline)
            self._journal.attempt(ring.target, ring.generation, level)
        ring.attempted = level
        async with asyncio.timeout(IO_TIMEOUT):
            await setter(ring.target, level)
        deadline = asyncio.get_running_loop().time() + min(IO_TIMEOUT, self._start_timeout)
        while True:
            observed = await self._read_volume(ring.target)
            if self._matches(observed, level):
                if self._journal is not None and ring.generation is not None:
                    self._journal.confirm(ring.target, ring.generation, level)
                ring.written, ring.attempted = level, None
                return True
            if observed is not None and not self._matches(observed, current):
                ring.volume_released = True
                return False
            if asyncio.get_running_loop().time() >= deadline:
                # A delayed write can still land; cleanup rechecks the attempted value.
                return False
            await asyncio.sleep(0.05)

    async def _restore_volume(self, ring: _Ring) -> None:
        if ring.baseline is None or ring.displaced or ring.volume_released:
            return
        owned = (ring.written, ring.attempted)
        if owned == (None, None):
            return
        current = await self._read_volume(ring.target)
        if current is None:
            raise RuntimeError("player volume unavailable during cleanup")
        if self._matches(current, ring.baseline):
            return
        if not any(level is not None and self._matches(current, level) for level in owned):
            return  # a distinct external volume wins
        if not await self._set_volume(ring, ring.baseline):
            raise RuntimeError("volume restore not observed")

    async def reconcile_volume(self, target: str) -> None:
        """Retry an uncertain restore on an idle target, never replay or stop audio."""
        ring = self._pending_restore.get(self._policy.check(target))
        if ring is None and self._journal is not None:
            record = self._journal.get(target)
            if record is not None:
                ring = _Ring(
                    target,
                    generation=record["generation"],
                    baseline=record["baseline"],
                    written=record["written"],
                    attempted=record["attempted"],
                )
                self._pending_restore[target] = ring
        if ring is None:
            return
        async with self._media_lock:
            if self._pending_restore.get(target) is not ring:
                return
            try:
                state = await self._state(target)
                if state in PLAYING_STATES or state == "paused":
                    _LOGGER.warning("Alarm volume ownership not recoverable for active %s", target)
                elif state in {"idle", "off", "standby"}:
                    await self._restore_volume(ring)
                    if self._journal is not None and ring.generation is not None:
                        self._journal.clear(target, ring.generation)
                    self._pending_restore.pop(target, None)
            except Exception as error:
                _LOGGER.warning(
                    "Alarm volume retry failed for %s: %s", target, type(error).__name__
                )

    def prepare_ring(self) -> None:
        """Forget a previous outcome before the dispatcher starts a new generation."""
        self._ring = None
        self._stop_requested.clear()

    async def _finish(self, ring: _Ring) -> PlaybackOutcome:
        result = PlaybackOutcome(PlaybackStatus.STOP_UNCONFIRMED, ring.target, "no_playback")
        async with self._media_lock:
            try:
                state = await self._state(ring.target)
                if ring.started and state in {"idle", "off", "standby"}:
                    result = PlaybackOutcome(
                        PlaybackStatus.STOPPED_CONFIRMED, ring.target, "already_idle"
                    )
                    return result
                owned = await self._identify_media(ring)
                if owned is None:
                    # Unobservable identity is a refusal, not a licence to stop: the
                    # playing content may be somebody else's.
                    return PlaybackOutcome(
                        PlaybackStatus.STOP_UNCONFIRMED, ring.target, "media_identity_unavailable"
                    )
                if not owned:
                    return PlaybackOutcome(
                        PlaybackStatus.STOP_UNCONFIRMED, ring.target, "media_replaced"
                    )
                if ring.play_attempted and not ring.started:
                    # A service timeout can occur after the player accepted the
                    # command. Stop only when the current media identifies the
                    # exact requested alarm; unknown ownership remains untouched.
                    reader = getattr(self._client, "get_media_id", None)
                    if reader is not None and await self._state(ring.target) in PLAYING_STATES:
                        async with asyncio.timeout(IO_TIMEOUT):
                            observed = await reader(ring.target)
                        if observed == self._media[1]:
                            ring.started = True
                            ring.media_id = observed
                if ring.started:
                    result = await self._stop_transport(
                        ring.target, wait=self._stop_timeout, interval=0.1
                    )
                    if result.status is not PlaybackStatus.STOPPED_CONFIRMED:
                        _LOGGER.warning(
                            "Alarm stop not confirmed for %s: %s", ring.target, result.detail
                        )
                return result
            finally:
                # Do not raise the level on a still-playing alarm after a failed stop.
                can_restore = result.status is PlaybackStatus.STOPPED_CONFIRMED
                if not ring.started:
                    try:
                        can_restore = not ring.play_attempted or (
                            await self._state(ring.target) in {"idle", "off", "standby"}
                        )
                    except Exception as error:
                        _LOGGER.warning("Alarm cleanup state unreadable: %s", type(error).__name__)
                if can_restore:
                    try:
                        await self._restore_volume(ring)
                        if self._journal is not None and ring.generation is not None:
                            self._journal.clear(ring.target, ring.generation)
                    except Exception as error:
                        self._pending_restore[ring.target] = ring
                        _LOGGER.warning(
                            "Alarm volume restore failed for %s: %s",
                            ring.target,
                            type(error).__name__,
                        )
                elif (ring.written is not None or ring.attempted is not None) and not (
                    ring.displaced or ring.volume_released
                ):
                    self._pending_restore[ring.target] = ring
                    _LOGGER.warning(
                        "Alarm volume restore deferred for %s: stop unconfirmed", ring.target
                    )

    def _cleanup(self, ring: _Ring) -> asyncio.Task[PlaybackOutcome]:
        ring.stopping = True
        self._stop_requested.set()
        if ring.cleanup is None:
            ring.cleanup = asyncio.create_task(self._finish(ring))
            ring.cleanup.add_done_callback(self._observe_cleanup)
        return ring.cleanup

    @staticmethod
    def _observe_cleanup(task: asyncio.Task[PlaybackOutcome]) -> None:
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.warning("Alarm cleanup failed: %s", type(task.exception()).__name__)

    async def stop_ring(self, entity_id: str) -> PlaybackOutcome:
        target = self._policy.check(entity_id)
        ring = self._ring
        if ring is None or ring.target != target:
            return PlaybackOutcome(PlaybackStatus.STOP_UNCONFIRMED, target, "no_owned_playback")
        return await asyncio.shield(self._cleanup(ring))

    async def duck(self, seconds: float) -> PlaybackOutcome:
        ring = self._ring
        if ring is None or not ring.started or ring.stopping:
            return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, "", "not_ringing")
        target = ring.target
        if not math.isfinite(seconds) or not 0 < seconds <= 120:
            return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, target, "invalid_seconds")
        async with self._media_lock:
            if ring.stopping or ring is not self._ring:
                return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, target, "not_ringing")
            try:
                if await self._state(target) not in PLAYING_STATES | {"idle"}:
                    return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, target, "not_available")
                owned = await self._identify_media(ring)
                if owned is None:
                    return PlaybackOutcome(
                        PlaybackStatus.DUCK_FAILED, target, "media_identity_unavailable"
                    )
                if not owned:
                    return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, target, "media_replaced")
                # Remember a mute attempt even if observation fails: no proof of
                # ducking is not proof that the player stayed at the previous level.
                ring.duck_active = True
                confirmed = await self._set_volume(ring, 0.0)
            except Exception as error:
                _LOGGER.warning("Alarm duck failed for %s: %s", target, type(error).__name__)
                return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, target, type(error).__name__)
            if not confirmed or ring.stopping:
                return PlaybackOutcome(
                    PlaybackStatus.DUCK_UNCONFIRMED, target, "mute_not_confirmed"
                )
            ring.duck_until = max(ring.duck_until, asyncio.get_running_loop().time() + seconds)
            return PlaybackOutcome(PlaybackStatus.DUCK_CONFIRMED, target, "reported_volume_zero")

    async def _require_start(self, ring: _Ring) -> None:
        ring.play_attempted = True
        outcome = await self.start(ring.target)
        if outcome.status is PlaybackStatus.FAILED:
            raise RuntimeError(f"play_media failed: {outcome.detail}")
        ring.started = True
        observed = await self.confirm_playing(ring.target, wait=self._start_timeout, interval=0.05)
        if observed.status is not PlaybackStatus.PLAYING_CONFIRMED:
            raise RuntimeError(f"playback not confirmed: {observed.detail}")
        # At start, unknown identity is expected (a player may expose none); only a
        # positive mismatch is a failure. Stop and duck refuse unknown identity later.
        if await self._identify_media(ring) is False:
            raise RuntimeError("alarm media replaced")

    async def play(self, entity_id: str) -> PlaybackOutcome:
        """Own one alarm ring; perform cleanup exactly once even on cancellation."""
        target = self._policy.check(entity_id)
        if self._ring is not None and (self._ring.cleanup is None or not self._ring.cleanup.done()):
            raise RuntimeError("previous alarm cleanup is still active")
        await self.reconcile_volume(target)
        if target in self._pending_restore:
            raise RuntimeError("previous alarm volume restore is pending")
        self.prepare_ring()
        ring = self._ring = _Ring(target)
        try:
            async with self._media_lock:
                allowed_states = (
                    {"idle", "off", "standby", "paused"}
                    if self._allow_paused
                    else {"idle", "off", "standby"}
                )
                if await self._state(target) not in allowed_states:
                    raise RuntimeError("alarm target is busy or unavailable")
                if self._volume is not None and not await self._set_volume(ring, self._volume):
                    raise RuntimeError("ring volume not confirmed")
                if not ring.stopping:
                    await self._require_start(ring)
            await self._ring_phase(ring)
        finally:
            finished = await asyncio.shield(self._cleanup(ring))
        return finished

    async def _ring_phase(self, ring: _Ring) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._max_seconds
        phase_end = loop.time() + self._cycle_on
        audible = True
        while not ring.stopping and loop.time() < deadline:
            try:
                await asyncio.wait_for(self._stop_requested.wait(), timeout=0.1)
            except TimeoutError:
                pass
            async with self._media_lock:
                now = loop.time()
                if ring.stopping or now >= deadline:
                    break
                state = await self._state(ring.target)
                if state == "idle" and not self._repeat:
                    return
                if not await self._identify_media(ring):
                    raise RuntimeError("alarm media replaced")
                if state not in PLAYING_STATES | {"idle"}:
                    raise RuntimeError("alarm playback interrupted or unavailable")
                if now < ring.duck_until:
                    continue
                if ring.duck_active:
                    level = self._volume if self._volume is not None else ring.baseline
                    if level is not None:
                        current = await self._read_volume(ring.target)
                        if current is None:
                            raise RuntimeError("duck recovery volume unavailable")
                        # A mute refused by the player need not be undone.
                        if not self._matches(current, level):
                            if not await self._set_volume(ring, level):
                                raise RuntimeError("post-duck ring volume not confirmed")
                    ring.duck_active = False
                    audible = True
                    phase_end = now + self._cycle_on
                    if not ring.stopping and (self._repeat or state == "idle"):
                        await self._require_start(ring)
                    continue
                if self._cycle_off and now >= phase_end:
                    audible = not audible
                    level = self._volume if audible else 0.0
                    if level is None:
                        raise RuntimeError("ring cycle has no volume")
                    if not await self._set_volume(ring, level):
                        raise RuntimeError("ring phase volume not confirmed")
                    phase_end = now + (self._cycle_on if audible else self._cycle_off)
                if state == "idle" and self._repeat and audible and not ring.stopping:
                    await self._require_start(ring)

    async def stop_request(self) -> bool:
        """Compatibility helper; only a positively observed stop returns True."""
        if self._ring is None:
            return False
        outcome = await self.stop_ring(self._ring.target)
        return outcome.status is PlaybackStatus.STOPPED_CONFIRMED
