"""Media adapter that separates command acceptance from audible playback.

A positive ``play_media`` return only proves that the service call was
accepted. Audible playback starts only when the entity reports a playing
state, and a stop is only "confirmed" when the entity stops reporting it.
Nothing here may claim physical silence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .devices import DevicePolicy

PLAYING_STATES = frozenset({"playing", "buffering"})


class PlaybackStatus(StrEnum):
    """Explicit, non-overlapping outcomes for one playback step."""

    ACCEPTED = "accepted"
    PLAYING_CONFIRMED = "playing_confirmed"
    START_UNCONFIRMED = "start_unconfirmed"
    STOPPED_CONFIRMED = "stopped_confirmed"
    STOP_UNCONFIRMED = "stop_unconfirmed"
    FAILED = "failed"


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
    ) -> None:
        if not 0 < max_seconds <= 3600:
            raise ValueError("max_seconds must be positive and at most 3600")
        self._client = client
        self._policy = policy
        self._media = media
        self._max_seconds = max_seconds
        self._start_timeout = min(max_seconds, 5.0)
        self._stop_timeout = min(max_seconds, 5.0)
        self._stop_requested = asyncio.Event()
        self._repeat = repeat

    @property
    def policy(self) -> DevicePolicy:
        return self._policy

    async def start(self, entity_id: str) -> PlaybackOutcome:
        """Request playback. Success means accepted, never audible."""
        target = self._policy.check(entity_id)
        try:
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
                state = await self._client.get_state(target)
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
        self._stop_requested.set()
        try:
            await self._client.stop(target)
        except Exception as error:
            return PlaybackOutcome(PlaybackStatus.FAILED, target, type(error).__name__)
        deadline = asyncio.get_running_loop().time() + max(wait, 0.0)
        detail = "stop_requested"
        while True:
            try:
                state = await self._client.get_state(target)
            except Exception as error:
                detail = f"state_unreadable:{type(error).__name__}"
            else:
                detail = f"state={state}"
                if str(state).lower() not in PLAYING_STATES:
                    return PlaybackOutcome(PlaybackStatus.STOPPED_CONFIRMED, target, detail)
            if asyncio.get_running_loop().time() >= deadline:
                return PlaybackOutcome(PlaybackStatus.STOP_UNCONFIRMED, target, detail)
            await asyncio.sleep(min(interval, max(wait, 0.0)))

    # --- contract used by nabu_core.dispatch.SerialDispatcher -----------------
    async def play(self, entity_id: str) -> None:
        """Ring for at most ``max_seconds`` and then stop.

        The call deliberately occupies the serial channel for the ring phase:
        returning right after the command would end the dispatcher cycle while
        the speaker is still playing, and a later stop would have nothing to
        stop. A stop request shortens the ring phase instead of racing it.
        """
        target = self._policy.check(entity_id)
        self._stop_requested.clear()
        outcome = await self.start(target)
        if outcome.status is PlaybackStatus.FAILED:
            raise RuntimeError(f"play_media failed: {outcome.detail}")

        await self.confirm_playing(target, wait=self._start_timeout, interval=0.2)

        deadline = asyncio.get_running_loop().time() + self._max_seconds
        while asyncio.get_running_loop().time() < deadline:
            if self._stop_requested.is_set():
                break
            try:
                await asyncio.wait_for(self._stop_requested.wait(), timeout=0.25)
            except TimeoutError:
                pass
            if self._stop_requested.is_set():
                break
            if not self._repeat:
                continue
            try:
                state = await self._client.get_state(target)
            except Exception:  # noqa: BLE001, S112 - a failed probe must not end the ring
                continue
            if str(state).lower() not in PLAYING_STATES:
                try:
                    await self.start(target)
                except Exception:  # noqa: BLE001, S112 - one failed retry is not fatal
                    continue

        # The ring phase ended: either it ran its course or a stop arrived.
        # Both lead to an explicit stop request; "confirmed" stays separate.
        await self.stop(target, wait=self._stop_timeout, interval=0.2)

    async def stop_request(self) -> bool:
        """Bounded stop helper for the dispatcher; True only on confirmation."""
        target = self._policy.allowed and sorted(self._policy.allowed)[0]
        outcome = await self.stop(target, wait=2.0, interval=0.2)
        return outcome.status is PlaybackStatus.STOPPED_CONFIRMED
