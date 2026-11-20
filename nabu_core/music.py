"""Put the music aside for the duration of an alarm, then give it back where it was.

Only playback that was running *before* the alarm is touched, and only while it is
still the same media: if somebody else took the speaker over in the meantime, resuming
would start a second stream on top of theirs. Everything is injected and honest about
what could be observed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

_LOGGER = logging.getLogger(__name__)
IO_TIMEOUT = 2.0
PLAYING_STATES = frozenset({"playing", "buffering"})


@dataclass(frozen=True, slots=True)
class MusicToken:
    """What was playing, so it can be resumed exactly once."""

    entity_id: str
    position: float | None = None
    media_id: str | None = None
    volume: float | None = None


class MusicClient(Protocol):
    async def get_state(self, entity_id: str) -> str: ...

    async def get_attributes(self, entity_id: str) -> dict: ...

    async def pause(self, entity_id: str) -> None: ...

    async def play(self, entity_id: str) -> None: ...


class MusicController(Protocol):
    async def pause(self) -> MusicToken | None: ...

    async def resume(self, token: MusicToken) -> bool: ...


class HaMusicController:
    """Pause and resume the player that Music Assistant feeds, through Home Assistant."""

    def __init__(self, client: MusicClient, *, entity_id: str, seek: bool = True) -> None:
        if not entity_id.startswith("media_player."):
            raise ValueError("music entity must be a media_player")
        self._client = client
        self._entity = entity_id
        self._seek = seek

    async def _state(self) -> str:
        async with asyncio.timeout(IO_TIMEOUT):
            return str(await self._client.get_state(self._entity)).lower()

    async def _attributes(self) -> dict:
        try:
            async with asyncio.timeout(IO_TIMEOUT):
                return dict(await self._client.get_attributes(self._entity) or {})
        except Exception as error:  # attributes are a bonus, not a precondition
            _LOGGER.warning("Music attributes unreadable: %s", type(error).__name__)
            return {}

    async def pause(self) -> MusicToken | None:
        """Pause our music. Returns None when there was nothing of ours to pause."""
        state = await self._state()
        if state not in PLAYING_STATES:
            return None  # silence, a foreign stream or a stopped queue
        attributes = await self._attributes()
        token = MusicToken(
            entity_id=self._entity,
            position=_number(attributes.get("media_position")),
            media_id=attributes.get("media_content_id") or attributes.get("media_title"),
            volume=_number(attributes.get("volume_level")),
        )
        async with asyncio.timeout(IO_TIMEOUT):
            await self._client.pause(self._entity)
        for _ in range(10):
            if await self._state() not in PLAYING_STATES:
                return token
            await asyncio.sleep(0.1)
        _LOGGER.warning("Music did not report paused; alarm must not start on top of it")
        return None

    async def resume(self, token: MusicToken) -> bool:
        """Bring back exactly what was paused, unless somebody else owns the speaker now."""
        state = await self._state()
        if state in PLAYING_STATES:
            return False  # already playing: not ours to start twice
        attributes = await self._attributes()
        current = attributes.get("media_content_id") or attributes.get("media_title")
        if token.entity_id != self._entity or not token.media_id or current != token.media_id:
            _LOGGER.warning("Music resume skipped: media identity is missing or changed")
            return False
        async with asyncio.timeout(IO_TIMEOUT):
            await self._client.play(self._entity)
        for _ in range(15):
            if await self._state() in PLAYING_STATES:
                return True
            await asyncio.sleep(0.1)
        return False

    async def seek(self, token: MusicToken) -> bool:
        """Optional: the player may not support seeking; a seek failure is not a resume failure."""
        if not self._seek or token.position is None:
            return False
        seeker = getattr(self._client, "seek", None)
        if seeker is None:
            return False
        try:
            async with asyncio.timeout(IO_TIMEOUT):
                await seeker(token.entity_id, token.position)
            return True
        except Exception as error:
            _LOGGER.warning("Music seek failed: %s", type(error).__name__)
            return False


def _number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None
