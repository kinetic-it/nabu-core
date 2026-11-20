"""The registry must expose the startup volume reconciliation (F-10 wiring).

The fix only helps if the layer that Home Assistant sets up also hands the adapter to
something that runs once per setup. These tests pin the wiring, not the restore logic
(covered in ``tests/nabu_core/test_r08c04_startup_recovery.py``).
"""

from unittest.mock import AsyncMock

from custom_components.kinetic_voice_router.alarm_bridge import (
    CONF_ALARM_DB,
    CONF_ALARM_TARGETS,
    AlarmServiceRegistry,
    build_alarm_layer,
)
from custom_components.kinetic_voice_router.const import CONF_MODE

TARGET = "media_player.lab"


def alarm_options() -> dict:
    return {
        CONF_MODE: "active",
        CONF_ALARM_DB: "alarm.sqlite",
        CONF_ALARM_TARGETS: [TARGET],
    }


class Host:
    """Minimal host: config, services and a data dict — nothing else."""

    def __init__(self, tmp_path, *, with_tasks: bool = False):
        self.data = {}
        self.scheduled = []
        self.config = type(
            "Config",
            (),
            {
                "time_zone": "Europe/Berlin",
                "path": lambda _, name: str(tmp_path / name),
            },
        )()
        self.services = type("S", (), {"async_register": lambda *a, **k: None})()
        if with_tasks:

            def async_create_task(coro, *args, **kwargs):
                self.scheduled.append(coro)
                return coro

            self.async_create_task = async_create_task


class Entry:
    def __init__(self, options):
        self.options = options

    def async_on_unload(self, *_args, **_kwargs):
        return None


async def test_layer_exposes_the_adapter_for_recovery(tmp_path) -> None:
    _, registry = build_alarm_layer(Host(tmp_path), alarm_options())

    assert registry.adapter is not None
    assert hasattr(registry, "reconcile_pending_volume")


async def test_registry_runs_the_reconciliation(tmp_path) -> None:
    _, registry = build_alarm_layer(Host(tmp_path), alarm_options())
    registry.adapter = type("Adapter", (), {})()
    registry.adapter.reconcile_all = AsyncMock(return_value={TARGET: "restored"})

    assert await registry.reconcile_pending_volume() == {TARGET: "restored"}
    registry.adapter.reconcile_all.assert_awaited_once_with()


async def test_registry_without_adapter_is_harmless() -> None:
    registry = AlarmServiceRegistry(layer=None)

    assert await registry.reconcile_pending_volume() == {}


async def test_setup_schedules_the_recovery(tmp_path) -> None:
    from custom_components.kinetic_voice_router import _async_setup_alarm_services

    host = Host(tmp_path, with_tasks=True)
    await _async_setup_alarm_services(host, Entry(alarm_options()))

    assert len(host.scheduled) == 1, "startup recovery must be scheduled exactly once"
    assert await host.scheduled[0] == {}, "an empty journal means nothing to restore"


async def test_setup_survives_a_host_without_task_support(tmp_path) -> None:
    # Stub labs have no async_create_task; the hook must not break setup there.
    from custom_components.kinetic_voice_router import _async_setup_alarm_services

    host = Host(tmp_path)
    await _async_setup_alarm_services(host, Entry(alarm_options()))

    assert host.data, "the registry is stored even without a task helper"
