"""Contract tests for the Home Assistant alarm service registration.

These use a minimal fake service registry and fake service calls, not a real
Home Assistant. Green here means the wiring and the result contract are correct;
it is not a live Home Assistant result.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.kinetic_voice_router.alarm_bridge import (
    ALARM_DOMAIN,
    SERVICE_FIELDS,
    SYNC_SERVICES,
    AlarmServiceRegistry,
)
from nabu_core.alarm_engine import AlarmStore
from nabu_core.dispatch import SerialDispatcher
from nabu_core.ha_services import AlarmServiceLayer
from nabu_core.scheduler import Scheduler


class FakeServices:
    def __init__(self):
        self.registered = {}
        self.removed = []

    def async_register(self, domain, name, handler, schema=None):
        self.registered[(domain, name)] = handler

    def async_remove(self, domain, name):
        self.removed.append((domain, name))
        self.registered.pop((domain, name), None)


class FakeHass:
    def __init__(self):
        self.services = FakeServices()


class FakeCall:
    def __init__(self, data):
        self.data = data


class FakePlayer:
    def __init__(self):
        self.calls = []

    async def play(self, target):
        self.calls.append(target)

    async def stop(self, target):
        return True


def build(tmp_path):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    player = FakePlayer()
    scheduler = Scheduler(
        store, SerialDispatcher(player, allowed_targets={"wohnzimmer"}, max_seconds=5), "wohnzimmer"
    )
    layer = AlarmServiceLayer(
        store,
        stop_callback=lambda: scheduler.stop(datetime.now(UTC)),
        scheduler=scheduler,
    )
    hass = FakeHass()
    registry = AlarmServiceRegistry(layer)
    return hass, registry, layer, player


def test_all_services_are_registered_once(tmp_path):
    hass, registry, _, _ = build(tmp_path)

    registry.register(hass)
    registry.register(hass)

    assert len(hass.services.registered) == len(SERVICE_FIELDS)
    for name in SERVICE_FIELDS:
        assert (ALARM_DOMAIN, f"alarm_{name}") in hass.services.registered


@pytest.mark.asyncio
async def test_create_list_enable_disable_delete_via_service_calls(tmp_path):
    hass, registry, layer, _ = build(tmp_path)
    handlers = registry.register(hass)

    created = await handlers["create"](FakeCall({"in_minutes": 10, "name": "Test"}))
    assert created["ok"] is True
    alarm_id = created["data"]["id"]

    listed = await handlers["list"](FakeCall({}))
    assert listed["data"]["count"] == 1

    assert (await handlers["disable"](FakeCall({"id": alarm_id})))["data"]["enabled"] is False
    assert (await handlers["enable"](FakeCall({"id": alarm_id})))["data"]["enabled"] is True

    deleted = await handlers["delete"](FakeCall({"id": alarm_id}))
    assert deleted["ok"] is True
    assert (await handlers["list"](FakeCall({})))["data"]["count"] == 0


@pytest.mark.asyncio
async def test_status_exposes_missed_and_errors(tmp_path):
    hass, registry, layer, _ = build(tmp_path)
    handlers = registry.register(hass)
    layer.store.create(
        datetime(2026, 11, 18, 10, 0, tzinfo=UTC),
        now=datetime(2026, 11, 18, 9, 0, tzinfo=UTC),
    )

    await handlers["ring"](FakeCall({"now": datetime(2026, 11, 18, 12, 0, tzinfo=UTC).isoformat()}))
    status = await handlers["status"](FakeCall({}))

    assert status["data"]["missed"][0]["detail"] == "missed"
    assert status["data"]["free_slots"] == 9


@pytest.mark.asyncio
async def test_catch_up_configuration_via_service_call(tmp_path):
    hass, registry, layer, _ = build(tmp_path)
    handlers = registry.register(hass)
    alarm_id = (await handlers["create"](FakeCall({"in_minutes": 10})))["data"]["id"]

    narrowed = await handlers["configure_catch_up"](
        FakeCall({"id": alarm_id, "catchup_seconds": 45})
    )

    assert narrowed["ok"] is True
    assert narrowed["data"]["catchup_seconds"] == 45


@pytest.mark.asyncio
async def test_stop_service_never_claims_silence(tmp_path):
    hass, registry, layer, _ = build(tmp_path)
    handlers = registry.register(hass)

    result = await handlers["stop"](FakeCall({}))

    assert result["code"] == "STOP_REQUESTED"
    assert result["data"]["confirmed"] is False


@pytest.mark.asyncio
async def test_eleventh_alarm_rejected_through_service(tmp_path):
    hass, registry, _, _ = build(tmp_path)
    handlers = registry.register(hass)
    for index in range(10):
        await handlers["create"](FakeCall({"in_minutes": 10 + index}))

    eleventh = await handlers["create"](FakeCall({"in_minutes": 99}))

    assert eleventh["ok"] is False
    assert eleventh["code"] == "NO_FREE_SLOT"


def test_unregister_removes_every_service(tmp_path):
    hass, registry, _, _ = build(tmp_path)
    registry.register(hass)

    registry.unregister(hass)

    assert hass.services.registered == {}
    assert len(hass.services.removed) == len(SERVICE_FIELDS)


def test_registration_module_needs_no_home_assistant_import():
    import custom_components.kinetic_voice_router.alarm_bridge as module

    assert hasattr(module, "AlarmServiceRegistry")
    assert SYNC_SERVICES.isdisjoint({"ring", "stop"})
