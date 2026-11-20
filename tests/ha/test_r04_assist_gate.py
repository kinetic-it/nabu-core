"""Satellite event filtering and idle settling, without Home Assistant."""

from types import SimpleNamespace

import pytest

from custom_components.kinetic_voice_router.assist_gate import AssistGate


async def test_satellite_gate_uses_authoritative_state_and_unsubscribes():
    clock = [0.0]
    values = {"assist_satellite.lab": SimpleNamespace(state="idle")}
    listeners = {}

    class Bus:
        def async_listen(self, event_type, listener):
            listeners[event_type] = listener
            return lambda: listeners.pop(event_type)

    class States:
        def get(self, entity_id):
            return values.get(entity_id)

    hass = SimpleNamespace(bus=Bus(), states=States())
    gate = AssistGate(hass, "assist_satellite.lab", clock=lambda: clock[0])
    with pytest.raises(RuntimeError):
        await gate.interaction_active()
    gate.register()
    assert await gate.interaction_active() is False
    values[gate.entity_id] = SimpleNamespace(state="listening")
    listeners["state_changed"](SimpleNamespace(data={"entity_id": gate.entity_id}))
    assert await gate.interaction_active() is True
    values[gate.entity_id] = SimpleNamespace(state="idle")
    listeners["state_changed"](SimpleNamespace(data={"entity_id": gate.entity_id}))
    assert await gate.interaction_active() is True
    clock[0] = 1.1
    assert await gate.interaction_active() is False
    values[gate.entity_id] = SimpleNamespace(state="unavailable")
    with pytest.raises(RuntimeError):
        await gate.interaction_active()
    gate.unregister()
    assert not listeners
