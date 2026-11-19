from datetime import UTC, datetime, timedelta

import pytest

from custom_components.kinetic_voice_router import (
    ALARM_REGISTRY_KEY,
    _async_setup_alarm_services,
)
from custom_components.kinetic_voice_router.alarm_bridge import (
    ALARM_DOMAIN,
    CONF_ALARM_DB,
    CONF_ALARM_RING_SECONDS,
    CONF_ALARM_TARGETS,
    SERVICE_FIELDS,
)

# the integration uses its bundled copy, so patch exactly that class
from custom_components.kinetic_voice_router.nabu_core.player import MediaPlayerAdapter
from nabu_core.alarm_engine import AlarmStore


class FakeConfig:
    def __init__(self, root):
        self.time_zone = "Europe/Berlin"
        self._root = root

    def path(self, name):
        return str(self._root / name)


class FakeStates:
    def __init__(self):
        self._states = {}

    def get(self, entity_id):
        return self._states.get(entity_id)

    def async_all(self, domain=None):
        return [
            state
            for entity_id, state in self._states.items()
            if domain is None or entity_id.startswith(f"{domain}.")
        ]


class WiringHass:
    """The smallest object the alarm wiring needs."""

    def __init__(self, root):
        self.config = FakeConfig(root)
        self.services = FakeServices()
        self.states = FakeStates()
        self.data = {}
        self.loop = None

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeServices:
    def __init__(self):
        self.registered = {}
        self.removed = []

    def async_register(self, domain, name, handler, schema=None, **kwargs):
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


@pytest.mark.asyncio
async def test_adapter_repeats_the_sound_until_stopped(tmp_path, monkeypatch):
    """A short audio file must not end the ring: the adapter re-arms it.

    Without this the alarm played for as long as the media file (two seconds in
    the pilot) and a later stop had nothing left to stop.
    """
    captured = {}
    original = MediaPlayerAdapter.__init__

    def spy(self, *args, **kwargs):
        captured.update(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MediaPlayerAdapter, "__init__", spy)
    hass = WiringHass(tmp_path)
    entry = FakeEntry(
        {
            CONF_ALARM_DB: "alarms.sqlite",
            CONF_ALARM_TARGETS: ["media_player.wohnzimmer"],
            CONF_ALARM_RING_SECONDS: 45,
        }
    )

    await _async_setup_alarm_services(hass, entry)

    assert captured["repeat"] is True
    assert captured["max_seconds"] == 45.0


def test_pilot_label_matching_ignores_case_and_wecker_suffix(tmp_path):
    from custom_components.kinetic_voice_router.alarm_bridge import _pilot_filter

    store = AlarmStore(tmp_path / "alarms.sqlite")
    named = store.create(timedelta(hours=1), name="Pilot", now=datetime.now(UTC))
    matches = _pilot_filter("pilotwecker")

    assert matches(named) is True
    assert matches(store.create(timedelta(hours=1), name="Anderer", now=datetime.now(UTC))) is False


@pytest.mark.asyncio
async def test_bundle_duck_stop_and_journal_through_registered_ha_services(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from custom_components.kinetic_voice_router.alarm_bridge import CONF_ALARM_VOLUME

    target = "media_player.licensed_test"
    media = SimpleNamespace(state="idle", attributes={"volume_level": 0.5})
    starting = asyncio.Event()
    calls = []
    hass = WiringHass(tmp_path)
    hass.states._states[target] = media

    async def call_service(domain, service, data, *, blocking):
        assert domain == "media_player" and data["entity_id"] == target and blocking
        calls.append(service)
        if service == "play_media":
            media.state = "playing"
            media.attributes["media_content_id"] = "alarm.wav"
            starting.set()
        elif service == "media_stop":
            media.state = "idle"
        elif service == "volume_set":
            media.attributes["volume_level"] = data["volume_level"]

    hass.services.async_call = call_service
    entry = FakeEntry(
        {
            CONF_ALARM_DB: "alarms.sqlite",
            CONF_ALARM_TARGETS: [target],
            CONF_ALARM_RING_SECONDS: 2,
            CONF_ALARM_VOLUME: 0.3,
        }
    )
    await _async_setup_alarm_services(hass, entry)
    registry = hass.data[ALARM_REGISTRY_KEY]
    now = datetime.now(UTC)
    alarm = registry.layer.store.create(now, now=now - timedelta(seconds=1))

    async def invoke(name, **data):
        return await hass.services.registered[(ALARM_DOMAIN, f"alarm_{name}")](
            SimpleNamespace(data=data)
        )

    ring = asyncio.create_task(invoke("ring", now=now.isoformat()))
    await asyncio.wait_for(starting.wait(), 1)
    assert (await invoke("duck", seconds=12))["code"] == "DUCKED"
    assert media.attributes["volume_level"] == 0
    assert (await invoke("stop"))["data"]["confirmed"] is True
    await ring
    assert media.attributes["volume_level"] == 0.5
    assert calls == ["volume_set", "play_media", "volume_set", "media_stop", "volume_set"]
    [occurrence] = registry.layer.scheduler.occurrences()
    assert occurrence.alarm_id == alarm.id
    assert occurrence.detail == "stop_confirmed"
    assert (await invoke("duck", seconds=12))["code"] == "NOT_RINGING"
