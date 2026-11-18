import pytest

from custom_components.kinetic_voice_router import (
    ALARM_REGISTRY_KEY,
    _async_setup_alarm_services,
)
from custom_components.kinetic_voice_router.alarm_bridge import (
    ALARM_DOMAIN,
    CONF_ALARM_DB,
    CONF_ALARM_TARGETS,
    SERVICE_FIELDS,
)


class FakeConfig:
    def __init__(self, root):
        self.time_zone = "Europe/Berlin"
        self._root = root

    def path(self, name):
        return str(self._root / name)


class FakeState:
    def __init__(self, state):
        self.state = state


class WiringHass:
    def __init__(self, root):
        self.services = FakeServices()
        self.data = {}
        self.config = FakeConfig(root)
        self.states = self

    def get(self, entity_id):
        return FakeState("idle")


class FakeServices:
    def __init__(self):
        self.registered = {}
        self.removed = []

    def async_register(self, domain, name, handler, schema=None):
        self.registered[(domain, name)] = handler

    def async_remove(self, domain, name):
        self.removed.append((domain, name))
        self.registered.pop((domain, name), None)


class FakeEntry:
    def __init__(self, options):
        self.options = options
        self.unload_callbacks = []

    def async_on_unload(self, callback):
        self.unload_callbacks.append(callback)


@pytest.mark.asyncio
async def test_alarm_services_are_opt_in(tmp_path):
    hass = WiringHass(tmp_path)

    await _async_setup_alarm_services(hass, FakeEntry({}))

    assert hass.services.registered == {}
    assert ALARM_REGISTRY_KEY not in hass.data


@pytest.mark.asyncio
async def test_configuration_without_targets_is_refused_softly(tmp_path):
    hass = WiringHass(tmp_path)

    await _async_setup_alarm_services(hass, FakeEntry({CONF_ALARM_DB: "alarms.sqlite"}))

    assert hass.services.registered == {}
    assert ALARM_REGISTRY_KEY not in hass.data


@pytest.mark.asyncio
async def test_enabled_configuration_registers_every_service(tmp_path):
    hass = WiringHass(tmp_path)
    entry = FakeEntry(
        {CONF_ALARM_DB: "alarms.sqlite", CONF_ALARM_TARGETS: ["media_player.wohnzimmer"]}
    )

    await _async_setup_alarm_services(hass, entry)

    assert len(hass.services.registered) == len(SERVICE_FIELDS)
    assert (ALARM_DOMAIN, "alarm_create") in hass.services.registered
    registry = hass.data[ALARM_REGISTRY_KEY]
    registry.unregister(hass)
    assert hass.services.registered == {}
