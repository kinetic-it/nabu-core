"""One alarm session: pause the music, ring, keep ringing until stopped, put the room back.

The adapter (`nabu_core.player`) owns one playback and reports honest outcomes.
This module owns the *sequence* around it: music out of the way, the ring supervised
so a media file that ends early does not end the alarm, and a teardown that restores
what was taken. Everything is injected, so the whole thing runs in the offline lab.

Rules that come from the device work and must not be softened:

* A stop is only claimed when the player no longer reports playing (the adapter does
  that); a stop that cannot be confirmed never leads to "music on top of the alarm".
* Ringing is not a one-shot: if the player goes idle while the alarm is still due, the
  session rings again a bounded number of times and then asks for a human.
* A failed start leaves no alarm behind and does not swallow the music.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

_LOGGER = logging.getLogger(__name__)

IDLE_STATES = frozenset({"idle", "off", "standby"})
PLAYING_STATES = frozenset({"playing", "buffering"})


class SessionOutcome(StrEnum):
    STOPPED = "stopped"
    RANG_OUT = "rang_out"
    START_FAILED = "start_failed"
    STOP_UNCONFIRMED = "stop_unconfirmed"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True, slots=True)
class SessionReport:
    outcome: SessionOutcome
    detail: str = ""
    restarts: int = 0
    music_resumed: bool | None = None
    warnings: tuple[str, ...] = ()


class MusicController(Protocol):
    """What the session needs from Music Assistant (or whatever plays the music)."""

    async def pause(self) -> object | None: ...

    async def resume(self, token: object) -> bool: ...


@dataclass
class _MusicClaim:
    token: object
    resumed: bool | None = None


@dataclass
class AlarmSession:
    """Ring once, supervised, and never leave the room louder than it was found."""

    ring: Callable[[], Awaitable[object]]
    stop: Callable[[], Awaitable[bool]]
    state_of: Callable[[], Awaitable[str]]
    interaction_active: Callable[[], Awaitable[bool]] | None = None
    music: MusicController | None = None
    poll: float = 0.5
    max_restarts: int = 2
    ring_seconds: float = 600.0
    restart_delay: float = 0.0
    _stop_requested: asyncio.Event = field(default_factory=asyncio.Event)
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _claim: _MusicClaim | None = None
    _warnings: list[str] = field(default_factory=list)
    _last_retry_detail: str | None = None

    def __post_init__(self) -> None:
        if not self.poll > 0:
            raise ValueError("poll must be positive")
        if self.max_restarts < 0:
            raise ValueError("max_restarts must not be negative")
        if not 0 < self.ring_seconds <= 3600:
            raise ValueError("ring_seconds must be between 0 and 3600")
        if not 0 <= self.restart_delay <= 10:
            raise ValueError("restart_delay must be between 0 and 10 seconds")

    async def request_stop(self) -> bool:
        """Ask the session to end now.

        Only signals; the run loop sends exactly one stop to the device and reports what
        it observed. Returns False when the session was already stopping.
        """
        if self._stop_requested.is_set():
            return False
        self._stop_requested.set()
        self._wake.set()
        return True

    async def run(self) -> SessionReport:
        claim = await self._pause_music()
        restarts = 0
        deadline = asyncio.get_running_loop().time() + self.ring_seconds
        try:
            while True:
                if self._stop_requested.is_set():
                    stopped = await self._quietly_stop()
                    outcome = SessionOutcome.STOPPED if stopped else SessionOutcome.STOP_UNCONFIRMED
                    return await self._finish(outcome, "stop_requested", restarts)
                if asyncio.get_running_loop().time() >= deadline:
                    if self._last_retry_detail is not None:
                        # The alarm never got through: saying "rang out" would claim
                        # a ring that never happened.
                        return await self._finish(
                            SessionOutcome.NEEDS_ATTENTION, self._last_retry_detail, restarts
                        )
                    return await self._finish(
                        SessionOutcome.RANG_OUT, "ring_seconds_elapsed", restarts
                    )
                if self.interaction_active is not None:
                    active = await self._interaction()
                    if active is True:
                        if asyncio.get_running_loop().time() >= deadline:
                            return await self._finish(
                                SessionOutcome.RANG_OUT, "ring_seconds_elapsed", restarts
                            )
                        try:
                            await asyncio.wait_for(self._wake.wait(), self.poll)
                        except TimeoutError:
                            pass
                        continue
                ring_task = asyncio.create_task(self._ring_once())
                reason = await self._supervise(ring_task, deadline)
                if reason is not None:
                    # Whatever ended the ring, the device gets exactly one stop request here.
                    stopped = await self._quietly_stop()
                    await self._settle_task(ring_task)
                    if reason == "stop_requested":
                        outcome = (
                            SessionOutcome.STOPPED if stopped else SessionOutcome.STOP_UNCONFIRMED
                        )
                        return await self._finish(outcome, "stop_requested", restarts)
                    if reason == "rang_out":
                        return await self._finish(
                            SessionOutcome.RANG_OUT, "ring_seconds_elapsed", restarts
                        )
                    if restarts >= self.max_restarts:
                        _LOGGER.warning(
                            "Alarm restarts exhausted (%s) after %d attempts", reason, restarts
                        )
                        return await self._finish(SessionOutcome.NEEDS_ATTENTION, reason, restarts)
                    restarts += 1
                    _LOGGER.info(
                        "Alarm ring ended early (%s); restarting (%d/%d)",
                        reason,
                        restarts,
                        self.max_restarts,
                    )
                    await self._restart_pause(deadline)
                    continue
                verdict = self._ring_result(ring_task)
                if verdict is None:
                    self._last_retry_detail = None  # a ring did happen
                    if self._stop_requested.is_set():
                        stopped = await self._quietly_stop()
                        outcome = (
                            SessionOutcome.STOPPED if stopped else SessionOutcome.STOP_UNCONFIRMED
                        )
                        return await self._finish(outcome, "stop_requested", restarts)
                    if restarts >= self.max_restarts:
                        return await self._finish(
                            SessionOutcome.NEEDS_ATTENTION, "ring_finished", restarts
                        )
                    restarts += 1
                    await self._restart_pause(deadline)
                    continue
                if verdict[0] is SessionOutcome.START_FAILED and self._retryable(verdict[1]):
                    # A refused start may have been accepted once; clear it first.
                    await self._quietly_stop()
                    if restarts >= self.max_restarts or (
                        asyncio.get_running_loop().time() >= deadline
                    ):
                        return await self._finish(
                            SessionOutcome.NEEDS_ATTENTION, verdict[1], restarts
                        )
                    restarts += 1
                    self._last_retry_detail = verdict[1]
                    _LOGGER.warning(
                        "Alarm could not ring yet (%s); retrying (%d/%d)",
                        verdict[1],
                        restarts,
                        self.max_restarts,
                    )
                    await self._restart_pause(deadline)
                    continue
                if verdict[0] is SessionOutcome.NEEDS_ATTENTION and self._retryable(verdict[1]):
                    if restarts >= self.max_restarts or (
                        asyncio.get_running_loop().time() >= deadline
                    ):
                        return await self._finish(
                            SessionOutcome.NEEDS_ATTENTION, verdict[1], restarts
                        )
                    restarts += 1
                    self._last_retry_detail = verdict[1]
                    _LOGGER.warning(
                        "Alarm blocked (%s); retrying (%d/%d)",
                        verdict[1],
                        restarts,
                        self.max_restarts,
                    )
                    await self._restart_pause(deadline)
                    continue
                if verdict[0] is SessionOutcome.START_FAILED:
                    await self._quietly_stop()  # a refused start may have been accepted once
                return await self._finish(verdict[0], verdict[1], restarts)
        finally:
            await self._resume_music(claim)

    async def _restart_pause(self, deadline: float) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        delay = min(self.restart_delay, max(remaining, 0.0))
        if delay:
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except TimeoutError:
                pass

    @staticmethod
    async def _settle_task(task: asyncio.Task) -> None:
        """Let a replaced ring call finish (or drop it) so nothing leaks into the next round."""
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            _LOGGER.debug("Ring task cancelled during cleanup")
        except Exception:
            _LOGGER.exception("Ring task failed during cleanup")

    async def _ring_once(self) -> object:
        return await self.ring()

    @staticmethod
    def _ring_result(ring_task: asyncio.Task) -> tuple[SessionOutcome, str] | None:
        """Translate what the ring call returned; None means "it ended the way it should"."""
        try:
            result = ring_task.result()
        except asyncio.CancelledError:
            return SessionOutcome.NEEDS_ATTENTION, "ring_cancelled"
        except Exception as error:
            return SessionOutcome.START_FAILED, f"{type(error).__name__}:{error}"
        outcome = str(getattr(result, "status", None) or getattr(result, "outcome", "") or "")
        detail = str(getattr(result, "detail", "") or "")
        if outcome == "completed":
            return None
        if outcome == "failed":
            return SessionOutcome.START_FAILED, f"ring_failed:{detail}"
        if outcome == "timeout":
            if "unconfirmed" in detail:
                return SessionOutcome.STOP_UNCONFIRMED, "ring_timeout"
            return None
        if outcome == "unsettled":
            return SessionOutcome.NEEDS_ATTENTION, "previous_call_active"
        if outcome == "blocked":
            return SessionOutcome.NEEDS_ATTENTION, f"blocked:{detail or 'target_busy'}"
        return SessionOutcome.NEEDS_ATTENTION, f"unexpected_ring_result:{outcome}"

    async def _interaction(self) -> bool | None:
        """Is somebody talking to the satellite right now?

        ``True`` only for a confirmed dialogue; ``None`` when the satellite state is
        unreadable or not a boolean. Unknown must never silence the alarm: an
        unverified state is not evidence that somebody is talking, and an alarm that
        stops itself because of a stale device state is worse than one that rings
        while it could have paused.
        """
        if self.interaction_active is None:
            return False
        try:
            active = await asyncio.wait_for(self.interaction_active(), timeout=1.0)
        except Exception as error:
            self._note(f"interaction_state_unavailable:{type(error).__name__}")
            return None
        if isinstance(active, bool):
            return active
        self._note("interaction_state_unknown")
        return None

    def _note(self, warning: str) -> None:
        if warning not in self._warnings:
            self._warnings.append(warning)

    @staticmethod
    def _retryable(detail: str) -> bool:
        """Is the player merely unavailable right now, or is the alarm itself broken?

        Only availability is retried. A real start failure (missing media, refused
        service call) must still be reported at once instead of being repeated.
        """
        lowered = detail.lower()
        return any(
            token in lowered
            for token in (
                "blocked",
                "player_not_idle",
                "busy",
                "unavailable",
                "not_ready",
                "not ready",
            )
        )

    async def _supervise(self, ring_task: asyncio.Task, deadline: float) -> str | None:
        """Wait for the ring to end. Returns why it ended early, or None if it ran out."""
        loop = asyncio.get_running_loop()
        while True:
            if self._stop_requested.is_set():
                return "stop_requested"
            if loop.time() >= deadline:
                return "rang_out"
            if ring_task.done():
                return None
            try:  # react to a stop at once, not after the poll
                await asyncio.wait_for(self._wake.wait(), self.poll)
            except TimeoutError:
                pass
            if ring_task.done():
                return None
            if self.interaction_active is not None:
                active = await self._interaction()
                if active is True:
                    return "interaction_active"
            try:
                state = str(await self.state_of()).lower()
            except Exception as error:
                _LOGGER.debug("Alarm player state unreadable: %s", type(error).__name__)
                continue
            if state in IDLE_STATES:
                # The file ended (or the player dropped it) while the alarm is still due.
                return f"player_idle:{state}"

    async def _quietly_stop(self) -> bool:
        try:
            return bool(await self.stop())
        except Exception as error:
            _LOGGER.warning("Alarm stop failed: %s", type(error).__name__)
            return False

    async def _pause_music(self) -> _MusicClaim | None:
        if self.music is None:
            return None
        try:
            token = await self.music.pause()
        except Exception as error:
            _LOGGER.warning("Music pause failed: %s", type(error).__name__)
            return None
        if token is None:
            return None
        self._claim = _MusicClaim(token)
        return self._claim

    async def _resume_music(self, claim: _MusicClaim | None) -> None:
        if claim is None:
            return
        stopped = await self._state_is_quiet()
        if not stopped:
            # Two streams at once is exactly what this session exists to prevent.
            claim.resumed = False
            _LOGGER.warning("Music stays paused: the alarm is not confirmed stopped")
            return
        try:
            claim.resumed = bool(await self.music.resume(claim.token))
        except Exception as error:
            claim.resumed = False
            _LOGGER.warning("Music resume failed: %s", type(error).__name__)

    async def _state_is_quiet(self) -> bool:
        try:
            state = str(await self.state_of()).lower()
        except Exception:
            return False
        return state in IDLE_STATES | {"paused"}

    async def _finish(self, outcome: SessionOutcome, detail: str, restarts: int) -> SessionReport:
        return SessionReport(
            outcome=outcome,
            detail=detail,
            restarts=restarts,
            warnings=tuple(self._warnings),
        )
