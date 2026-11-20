"""HA alarm wiring must attach session supervision for an explicit satellite."""

from custom_components.kinetic_voice_router.alarm_bridge import build_alarm_layer


def test_satellite_option_creates_a_supervised_session(tmp_path):
    class Config:
        time_zone = "Europe/Berlin"

        def path(self, name):
            return str(tmp_path / name)

    class Hass:
        config = Config()

    layer, _ = build_alarm_layer(
        Hass(),
        {
            "alarm_db": "alarm.sqlite",
            "alarm_targets": ["media_player.lab"],
            "alarm_satellite": "assist_satellite.lab",
        },
    )
    assert layer.scheduler.session_factory is not None


async def test_foreign_playback_is_not_overwritten(tmp_path):
    class Config:
        time_zone = "Europe/Berlin"

        def path(self, name):
            return str(tmp_path / name)

    class State:
        state = "playing"

    class States:
        def get(self, entity_id):
            return State()

    class Hass:
        config = Config()
        states = States()

    layer, _ = build_alarm_layer(
        Hass(),
        {
            "alarm_db": "alarm.sqlite",
            "alarm_targets": ["media_player.lab"],
            "alarm_satellite": "assist_satellite.lab",
        },
    )
    session = layer.scheduler.session_factory("media_player.lab")
    result = await session.ring()
    assert result.status == "blocked"
    assert result.detail == "player_not_idle"
