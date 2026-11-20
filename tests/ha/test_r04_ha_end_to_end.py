"""End-to-end HA-shaped test of the stream ending before Assist listens."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.kinetic_voice_router.alarm_bridge import build_alarm_layer


async def test_wake_interruption_does_not_end_logical_alarm(tmp_path):
    values = {
        "media_player.lab": SimpleNamespace(state="idle", attributes={}),
        "assist_satellite.lab": SimpleNamespace(state="idle", attributes={}),
    }
    callbacks = {}
    plays = []
    played = asyncio.Event()

    class Config:
        time_zone = "UTC"

        def path(self, name):
            return str(tmp_path / name)

    class Bus:
        def async_listen(self, name, callback):
            callbacks[name] = callback
            return lambda: callbacks.pop(name, None)

    class States:
        def get(self, entity):
            return values.get(entity)

    class Services:
        def async_register(self, *args, **kwargs):
            pass

        def async_remove(self, *args):
            pass

        async def async_call(self, domain, name, data, **kwargs):
            entity = data["entity_id"]
            if name == "play_media":
                plays.append(data["media_content_id"])
                played.set()
                values[entity] = SimpleNamespace(
                    state="playing", attributes={"media_content_id": data["media_content_id"]}
                )
            elif name == "media_stop":
                values[entity] = SimpleNamespace(state="idle", attributes=values[entity].attributes)

    hass = SimpleNamespace(config=Config(), bus=Bus(), states=States(), services=Services())
    layer, registry = build_alarm_layer(
        hass,
        {
            "alarm_db": "alarm.sqlite",
            "alarm_targets": ["media_player.lab"],
            "alarm_satellite": "assist_satellite.lab",
            "alarm_ring_seconds": 15,
        },
    )
    registry.register(hass)
    now = datetime.now(UTC)
    layer.store.create(now, now=now - timedelta(seconds=1))
    task = asyncio.create_task(layer.scheduler.tick(now))
    await asyncio.wait_for(played.wait(), 2)
    values["media_player.lab"] = SimpleNamespace(state="idle", attributes={})
    await asyncio.sleep(0.05)  # stream ends before satellite reports listening
    values["assist_satellite.lab"] = SimpleNamespace(state="listening", attributes={})
    callbacks["state_changed"](SimpleNamespace(data={"entity_id": "assist_satellite.lab"}))
    await asyncio.sleep(0.7)
    assert len(plays) == 1
    assert layer.scheduler.occurrences()[0].status == "in_flight"
    values["assist_satellite.lab"] = SimpleNamespace(state="idle", attributes={})
    callbacks["state_changed"](SimpleNamespace(data={"entity_id": "assist_satellite.lab"}))
    played.clear()
    await asyncio.wait_for(played.wait(), 4)
    await layer.scheduler.stop(datetime.now(UTC))
    await asyncio.wait_for(task, 4)
    assert layer.scheduler.occurrences()[0].status == "skipped"
    registry.unregister(hass)
