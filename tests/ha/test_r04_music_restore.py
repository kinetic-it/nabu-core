"""Music pause and restore through the HA-shaped alarm layer."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.kinetic_voice_router.alarm_bridge import build_alarm_layer

SONG = "media-source://music/song.mp3"


def _hass(tmp_path, store):
    class Config:
        time_zone = "UTC"

        def path(self, name):
            return str(tmp_path / name)

    class Services:
        def async_register(self, *args, **kwargs):
            pass

        def async_remove(self, *args):
            pass

        async def async_call(self, domain, name, data, **kwargs):
            store["calls"].append(name)
            entity = data["entity_id"]
            if name == "media_pause":
                store["values"][entity] = SimpleNamespace(
                    state="paused", attributes={"media_content_id": SONG}
                )
            elif name == "play_media":
                store["played"].set()
                store["values"][entity] = SimpleNamespace(
                    state="playing",
                    attributes={"media_content_id": data["media_content_id"]},
                )
            elif name == "media_stop":
                store["values"][entity] = SimpleNamespace(
                    state="paused", attributes={"media_content_id": SONG}
                )
            elif name == "media_play":
                store["values"][entity] = SimpleNamespace(
                    state="playing", attributes={"media_content_id": SONG}
                )

    class States:
        def get(self, entity):
            return store["values"].get(entity)

    class Bus:
        def async_listen(self, name, callback):
            return lambda: None

    return SimpleNamespace(config=Config(), services=Services(), states=States(), bus=Bus())


async def test_music_is_paused_before_the_alarm_and_restored_after_stop(tmp_path):
    store = {
        "calls": [],
        "played": asyncio.Event(),
        "values": {
            "media_player.lab": SimpleNamespace(
                state="playing", attributes={"media_content_id": SONG}
            ),
            "assist_satellite.lab": SimpleNamespace(state="idle", attributes={}),
        },
    }
    hass = _hass(tmp_path, store)
    layer, registry = build_alarm_layer(
        hass,
        {
            "alarm_db": "alarm.sqlite",
            "alarm_targets": ["media_player.lab"],
            "alarm_satellite": "assist_satellite.lab",
            "alarm_music_entity": "media_player.lab",
            "alarm_ring_seconds": 20,
        },
    )
    now = datetime.now(UTC)
    registry.register(hass)
    layer.store.create(now, now=now - timedelta(seconds=1))
    task = asyncio.create_task(layer.scheduler.tick(now))
    await asyncio.wait_for(store["played"].wait(), 3)
    assert store["calls"].index("media_pause") < store["calls"].index("play_media")
    assert await layer.scheduler.stop(datetime.now(UTC)) is True
    await asyncio.wait_for(task, 5)
    assert store["calls"].count("media_play") == 1
    assert store["values"]["media_player.lab"].state == "playing"
    assert layer.scheduler.occurrences()[0].status == "skipped"
