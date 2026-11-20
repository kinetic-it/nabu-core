"""Observe one explicitly configured HA Assist satellite without starting a pipeline."""

from __future__ import annotations

import time
from collections.abc import Callable


class AssistGate:
    """Fail closed on unknown state and debounce state transitions to idle."""

    def __init__(self, hass, entity_id: str, *, clock: Callable[[], float] = time.monotonic):
        self.hass = hass
        self.entity_id = entity_id
        self.clock = clock
        self.last_change = float("-inf")
        self._unsubscribe = None

    def register(self) -> None:
        if self._unsubscribe is not None:
            return
        self._unsubscribe = self.hass.bus.async_listen("state_changed", self._on_event)

    def unregister(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _on_event(self, event) -> None:
        if event.data.get("entity_id") == self.entity_id:
            self.last_change = self.clock()

    async def interaction_active(self) -> bool:
        if self._unsubscribe is None:
            raise RuntimeError("satellite event listener unavailable")
        state = self.hass.states.get(self.entity_id)
        value = str(state.state).lower() if state is not None else "unavailable"
        if value == "idle":
            return self.clock() - self.last_change < 1.0
        if value in {"listening", "processing", "responding"}:
            return True
        raise RuntimeError("satellite state unavailable")
