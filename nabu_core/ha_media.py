"""Media client that speaks to Home Assistant through the service registry.

This lives in the domain package, not in the integration: the conversation
agent must never execute actions on its own, but the alarm channel is an
explicit, allowlisted output path that the owner switches on.
"""

from __future__ import annotations


class HomeAssistantMediaClient:
    """Media client backed by ``hass.services``; the entity state is the truth.

    ``play_media`` returns as soon as Home Assistant accepted the service call.
    Whether sound is actually audible is answered by observing the entity state
    in :class:`nabu_core.player.MediaPlayerAdapter`, never by assumption.
    """

    def __init__(self, hass, media_id: str = "alarm.wav", media_type: str = "music") -> None:
        self._hass = hass
        self._media_id = media_id
        self._media_type = media_type

    async def play_media(self, entity_id: str, media: tuple[str, str]) -> None:
        media_type, media_id = (
            media
            if isinstance(media, tuple) and len(media) == 2
            else (self._media_type, self._media_id)
        )
        await self._hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": entity_id,
                "media_content_id": media_id,
                "media_content_type": media_type,
            },
            blocking=True,
        )

    async def stop(self, entity_id: str) -> None:
        await self._hass.services.async_call(
            "media_player",
            "media_stop",
            {"entity_id": entity_id},
            blocking=True,
        )

    async def get_state(self, entity_id: str) -> str:
        state = self._hass.states.get(entity_id)
        return "unavailable" if state is None else str(state.state)

    def _attributes(self, entity_id: str):
        state = self._hass.states.get(entity_id)
        if state is None or state.state in {"unavailable", "unknown"}:
            return {}
        return state.attributes

    async def get_media_id(self, entity_id: str) -> str | None:
        value = self._attributes(entity_id).get("media_content_id")
        return value if isinstance(value, str) and value else None

    async def get_volume(self, entity_id: str) -> float | None:
        return self._attributes(entity_id).get("volume_level")

    async def set_volume(self, entity_id: str, level: float) -> None:
        await self._hass.services.async_call(
            "media_player",
            "volume_set",
            {"entity_id": entity_id, "volume_level": level},
            blocking=True,
        )
