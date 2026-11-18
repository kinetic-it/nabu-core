"""Home Assistant registration for the Nabu Core alarm engine.

The services registered here are thin adapters: every write goes through
:class:`nabu_core.ha_services.AlarmServiceLayer`, which commits to the single
authoritative SQLite store. No Home Assistant helper is used as a second
writable alarm store.

Home Assistant is not imported by this module. The integration passes its own
``hass`` object, which only has to provide ``services.async_register`` with the
usual ``(domain, name, handler, schema=...)`` signature. That keeps the module
importable in the offline laboratory.

The integration never calls Home Assistant services itself; the ringing media
call lives in :mod:`nabu_core.ha_media`, behind the device allowlist.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .nabu_core.ha_services import AlarmServiceLayer, ServiceResult

try:  # Home Assistant is optional here: the offline laboratory imports this module.
    from homeassistant.core import SupportsResponse
except ImportError:  # pragma: no cover - only without Home Assistant
    SupportsResponse = None  # type: ignore[assignment]

DOMAIN = "kinetic_voice_router"
ALARM_DOMAIN = "nabu_core"

_LOGGER = logging.getLogger(__name__)

#: Field names accepted by each service. The integration maps these onto native
#: Home Assistant selectors; no visual or dashboard decision is made here.
SERVICE_FIELDS: dict[str, tuple[str, ...]] = {
    "create": ("name", "when", "in_minutes", "weekdays", "catchup_seconds", "catchup_enabled"),
    "update": (
        "id",
        "name",
        "when",
        "in_minutes",
        "weekdays",
        "catchup_seconds",
        "catchup_enabled",
    ),
    "enable": ("id",),
    "disable": ("id",),
    "delete": ("id",),
    "list": (),
    "status": (),
    "stop": (),
    "ring": ("now",),
    "configure_catch_up": ("id", "catchup_seconds", "catchup_enabled"),
}

SYNC_SERVICES = frozenset(
    {"create", "update", "enable", "disable", "delete", "list", "status", "configure_catch_up"}
)
ASYNC_SERVICES = frozenset({"stop", "ring"})

CATCH_UP_DEFAULT_SECONDS = 300

#: Integration options that switch the alarm stack on. Without ``CONF_ALARM_DB``
#: nothing is registered, so existing installations are unaffected.
CONF_ALARM_DB = "alarm_db"
CONF_ALARM_TARGETS = "alarm_targets"
CONF_ALARM_DENIED = "alarm_denied"
CONF_ALARM_MEDIA = "alarm_media"
CONF_ALARM_MEDIA_TYPE = "alarm_media_type"
CONF_ALARM_RING_SECONDS = "alarm_ring_seconds"
CONF_ALARM_TICK_SECONDS = "alarm_tick_seconds"

#: A scheduler tick every 20 seconds keeps the firing delay short without busy
#: looping. One tick claims and plays at most one occurrence per alarm.
DEFAULT_TICK_SECONDS = 20


class AlarmServiceRegistry:
    """Registers the alarm services against a Home Assistant-like registry."""

    def __init__(self, layer: AlarmServiceLayer) -> None:
        self.layer = layer
        self.registered: dict[str, Any] = {}

    def _build(self, name: str):
        method = getattr(self.layer, name)

        async def handler(call) -> Mapping[str, Any]:
            data = dict(getattr(call, "data", {}) or {})
            result: ServiceResult = await method(data) if name in ASYNC_SERVICES else method(data)
            return result.as_dict()

        handler.__name__ = f"nabu_core_{name}"
        return handler

    def register(self, hass) -> dict[str, Any]:
        """Register every alarm service exactly once and return the handlers."""
        services = hass.services
        for name in SERVICE_FIELDS:
            if name in self.registered:
                continue
            handler = self._build(name)
            kwargs = {"schema": None}
            if SupportsResponse is not None:
                # Every alarm service answers with a result mapping; without this
                # flag Home Assistant rejects return_response calls.
                kwargs["supports_response"] = SupportsResponse.ONLY
            services.async_register(ALARM_DOMAIN, f"alarm_{name}", handler, **kwargs)
            self.registered[name] = handler
        return self.registered

    def unregister(self, hass) -> None:
        for name in list(self.registered):
            hass.services.async_remove(ALARM_DOMAIN, f"alarm_{name}")
            del self.registered[name]


def build_alarm_layer(hass, options: Mapping[str, Any]):
    """Build the alarm stack from integration options, or None when disabled.

    Requires ``nabu_core`` to be importable in the Home Assistant environment.
    Returns a tuple ``(layer, registry)`` so the caller can unregister later.
    """
    db_path = options.get(CONF_ALARM_DB)
    if not db_path:
        return None

    from .nabu_core.alarm_engine import AlarmStore
    from .nabu_core.devices import DevicePolicy
    from .nabu_core.dispatch import SerialDispatcher
    from .nabu_core.ha_media import HomeAssistantMediaClient
    from .nabu_core.ha_services import AlarmServiceLayer
    from .nabu_core.player import MediaPlayerAdapter
    from .nabu_core.scheduler import Scheduler

    allowed = tuple(options.get(CONF_ALARM_TARGETS, ()) or ())
    denied = tuple(options.get(CONF_ALARM_DENIED, ()) or ())
    if not allowed:
        raise ValueError("no alarm target is allowlisted")

    store = AlarmStore(hass.config.path(db_path), timezone_name=hass.config.time_zone)
    policy = DevicePolicy(allowed=allowed, denied=denied)
    media_id = options.get(CONF_ALARM_MEDIA, "alarm.wav")
    media_type = options.get(CONF_ALARM_MEDIA_TYPE, "music")
    ring_seconds = float(options.get(CONF_ALARM_RING_SECONDS, 120))
    adapter = MediaPlayerAdapter(
        HomeAssistantMediaClient(hass, media_id, media_type),
        policy,
        media=(media_type, media_id),
        max_seconds=ring_seconds,
        # An alarm has to keep ringing until somebody stops it: the configured
        # media file may be shorter than the ring phase.
        repeat=True,
    )
    dispatcher = SerialDispatcher(
        adapter, allowed_targets=set(allowed), max_seconds=ring_seconds + 10
    )
    scheduler = Scheduler(store, dispatcher, allowed[0])
    layer = AlarmServiceLayer(
        store, stop_callback=lambda: scheduler.stop(datetime.now(UTC)), scheduler=scheduler
    )
    return layer, AlarmServiceRegistry(layer)


def start_tick_timer(hass, layer, interval_seconds: float = DEFAULT_TICK_SECONDS):
    """Drive the scheduler from Home Assistant's event loop.

    Returns the unsubscribe callback. A failing tick is logged and never breaks
    the timer, so one broken alarm cannot stop the clock.
    """
    from datetime import timedelta

    from homeassistant.helpers.event import async_track_time_interval

    async def _tick(_now) -> None:
        try:
            await layer.scheduler.tick(datetime.now(UTC))
        except Exception:  # noqa: BLE001 - the timer must survive a bad tick
            _LOGGER.exception("alarm tick failed")

    return async_track_time_interval(hass, _tick, timedelta(seconds=interval_seconds))


def stop_tick_timer(unsubscribe) -> None:
    if unsubscribe is not None:
        unsubscribe()
