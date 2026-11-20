"""HA-shaped regression: stop works on the attribute set the Echo really reports.

The living-room Echo reports ``idle`` with nothing but ``volume_level``. R08.1
stopped nothing in that situation (R09 F-01) and aborted the ring when the media
id came back in another spelling (F-02). Both are pinned here through the real
chain: bridge -> scheduler -> session -> dispatcher -> adapter -> HA services.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.kinetic_voice_router.alarm_bridge import build_alarm_layer

TARGET = "media_player.lab"
SAT = "assist_satellite.lab"
ALARM = "media-source://media_source/local/alarm.wav"


def build_hass(tmp_path, reported_id, *, alarm_attributes=("volume_level",)):
    """A fake hass whose player only reports the attributes a real Echo reports."""

    class Config:
        time_zone = "Europe/Berlin"

        def path(self, name):
            return str(tmp_path / name)

    class Bus:
        def __init__(self):
            self.callbacks = {}

        def async_listen(self, name, callback):
            self.callbacks[name] = callback
            return lambda: self.callbacks.pop(name, None)

    class States:
        def __init__(self):
            self.values = {
                TARGET: SimpleNamespace(state="idle", attributes={"volume_level": 0.5}),
                SAT: SimpleNamespace(state="idle", attributes={}),
            }

        def get(self, entity):
            return self.values.get(entity)

    plays = []
    played = asyncio.Event()

    class Services:
        def async_register(self, *args, **kwargs):
            pass

        def async_remove(self, *args):
            pass

        async def async_call(self, domain, name, data, **kwargs):
            entity = data["entity_id"]
            current = dict(states.get(entity).attributes)
            if name == "play_media":
                plays.append(data["media_content_id"])
                current["volume_level"] = 0.3
                if "media_content_id" in alarm_attributes:
                    current["media_content_id"] = reported_id
                states.values[entity] = SimpleNamespace(state="playing", attributes=current)
                played.set()
            elif name == "media_stop":
                states.values[entity] = SimpleNamespace(state="idle", attributes=current)
            elif name == "volume_set":
                current["volume_level"] = data["volume_level"]
                states.values[entity] = SimpleNamespace(
                    state=states.get(entity).state, attributes=current
                )

    states = States()
    hass = SimpleNamespace(config=Config(), bus=Bus(), states=states, services=Services())
    layer, registry = build_alarm_layer(
        hass,
        {
            "alarm_db": "alarm.sqlite",
            "alarm_targets": [TARGET],
            "alarm_satellite": SAT,
            "alarm_ring_seconds": 15,
            "alarm_volume": 0.3,
            "alarm_media": ALARM,
        },
    )
    registry.register(hass)
    return hass, states, layer, registry, plays, played


async def run_alarm_and_stop(tmp_path, reported_id, **kwargs):
    hass, states, layer, registry, plays, played = build_hass(tmp_path, reported_id, **kwargs)
    now = datetime.now(UTC)
    layer.store.create(now, now=now - timedelta(seconds=1))
    tick = asyncio.create_task(layer.scheduler.tick(now))
    await asyncio.wait_for(played.wait(), 3)
    await asyncio.sleep(0.2)  # let the ring settle into its supervised phase
    await layer.scheduler.stop(datetime.now(UTC))
    await asyncio.wait_for(tick, 5)
    occurrence = layer.scheduler.occurrences()[0]
    final = states.get(TARGET)
    registry.unregister(hass)
    return occurrence, states, plays, final


@pytest.mark.parametrize(
    ("label", "reported_id", "attributes"),
    [
        ("echo_reports_no_media_id", None, ("volume_level",)),
        (
            "media_id_normalised",
            "media-source://local/alarm.wav",
            ("volume_level", "media_content_id"),
        ),
        (
            "media_id_as_http_url",
            "http://homeassistant.local:8123/local/alarm.wav",
            ("volume_level", "media_content_id"),
        ),
        ("media_id_identical", ALARM, ("volume_level", "media_content_id")),
    ],
)
async def test_stop_is_confirmed_whatever_shape_the_player_reports(
    tmp_path, label, reported_id, attributes
):
    occurrence, states, plays, final = await run_alarm_and_stop(
        tmp_path, reported_id, alarm_attributes=attributes
    )
    assert len(plays) == 1, label
    assert occurrence.status == "skipped", label
    assert "stop_confirmed" in (occurrence.detail or ""), label
    assert final.state == "idle", label
    assert final.attributes["volume_level"] == pytest.approx(0.5), label


async def test_a_foreign_stream_on_the_target_is_not_silenced(tmp_path):
    """Somebody else is playing when the alarm becomes due: nothing may be stopped."""
    hass, states, layer, registry, plays, played = build_hass(tmp_path, None)
    states.values[TARGET] = SimpleNamespace(
        state="playing", attributes={"volume_level": 0.5, "media_content_id": "spotify:track:x"}
    )
    now = datetime.now(UTC)
    layer.store.create(now, now=now - timedelta(seconds=1))
    tick = asyncio.create_task(layer.scheduler.tick(now))
    await asyncio.sleep(1.0)  # blocked, retried, still blocked
    await layer.scheduler.stop(datetime.now(UTC))
    await asyncio.wait_for(tick, 6)
    occurrence = layer.scheduler.occurrences()[0]
    registry.unregister(hass)
    assert plays == []
    assert states.get(TARGET).state == "playing"
    assert occurrence.status != "completed"
