"""Alarm options: reachable in a flow (F-06) and never dropped on save (F-07).

Field finding (2026-11-20): ``alarm_satellite`` and ``alarm_music_entity`` were
read by ``build_alarm_layer`` from ``entry.options`` but offered by no flow at all,
so the whole session path (speech interruption, snooze, bounded ring time,
restart recovery) was unreachable in a real installation. Worse, both option-saving
steps rebuilt the options dict from scratch, so one visit to the settings page would
have deleted the database path, the allowlisted targets, the pilot name, the ring
time, the volume and the media.

These tests pin the contract: every alarm option stays reachable, and saving one
option never removes another.
"""

import pytest
from conftest import TOKEN, ConfigEntry, FakeHass

from custom_components.kinetic_voice_router import config_flow
from custom_components.kinetic_voice_router.alarm_bridge import (
    CONF_ALARM_DB,
    CONF_ALARM_DENIED,
    CONF_ALARM_MEDIA,
    CONF_ALARM_MEDIA_TYPE,
    CONF_ALARM_MUSIC,
    CONF_ALARM_PILOT,
    CONF_ALARM_RING_SECONDS,
    CONF_ALARM_SATELLITE,
    CONF_ALARM_TARGETS,
    CONF_ALARM_TICK_SECONDS,
    CONF_ALARM_VOLUME,
    build_alarm_layer,
)
from custom_components.kinetic_voice_router.config_flow import (
    KineticVoiceRouterConfigFlow,
    KineticVoiceRouterOptionsFlow,
)
from custom_components.kinetic_voice_router.const import (
    CONF_MODE,
    CONF_SPEECH_MEMORY,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_TLS,
    DOMAIN,
)

SATELLITE = "assist_satellite.lab"
TARGET = "media_player.lab"

ALARM_OPTIONS = {
    CONF_ALARM_DB: "alarm.sqlite",
    CONF_ALARM_TARGETS: [TARGET],
    CONF_ALARM_DENIED: [],
    CONF_ALARM_PILOT: "pilot",
    CONF_ALARM_RING_SECONDS: 60,
    CONF_ALARM_VOLUME: 0.3,
    CONF_ALARM_MEDIA: "media-source://media_source/local/alarm.wav",
    CONF_ALARM_MEDIA_TYPE: "audio/wav",
    CONF_ALARM_TICK_SECONDS: 20,
}


def options_flow(hass: FakeHass, entry: ConfigEntry) -> KineticVoiceRouterOptionsFlow:
    flow = KineticVoiceRouterConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.config_entry = entry
    return flow


async def set_up(hass: FakeHass, options: dict | None = None) -> ConfigEntry:
    entry = hass.config_entries.add(
        ConfigEntry(
            domain=DOMAIN,
            entry_id="kvr-alarm-1",
            title="Kinetic Voice Router",
            data={
                CONF_URL: hass.companion.base_url,
                CONF_TOKEN: TOKEN,
                CONF_VERIFY_TLS: True,
            },
            options={CONF_MODE: "active", **ALARM_OPTIONS} if options is None else options,
            minor_version=3,
        )
    )
    await hass.config_entries.async_setup(entry.entry_id)
    return entry


def schema_marker(result: dict, key: str):
    for marker in result["data_schema"].schema:
        if str(marker) == key:
            return marker
    raise AssertionError(f"{key} missing from schema")


def test_alarm_option_keys_match_the_bridge() -> None:
    # config_flow mirrors the keys instead of importing alarm_bridge (a missing
    # nabu_core package must not break the conversation agent). This guard keeps
    # the two definitions in sync.
    assert config_flow.CONF_ALARM_SATELLITE == CONF_ALARM_SATELLITE
    assert config_flow.CONF_ALARM_MUSIC == CONF_ALARM_MUSIC
    assert config_flow.CONF_ALARM_RING_SECONDS == CONF_ALARM_RING_SECONDS
    assert config_flow.CONF_ALARM_VOLUME == CONF_ALARM_VOLUME
    assert config_flow.CONF_ALARM_MEDIA == CONF_ALARM_MEDIA
    assert config_flow.CONF_ALARM_MEDIA_TYPE == CONF_ALARM_MEDIA_TYPE
    assert config_flow.CONF_ALARM_TARGETS == CONF_ALARM_TARGETS


async def test_menu_offers_the_alarm_page(hass: FakeHass) -> None:
    entry = await set_up(hass)
    result = await options_flow(hass, entry).async_step_init()
    assert "alarms" in result["menu_options"]


async def test_saving_the_mode_keeps_every_alarm_option(hass: FakeHass) -> None:
    entry = await set_up(hass)
    result = await options_flow(hass, entry).async_step_settings({CONF_MODE: "observe"})

    assert result["type"] == "create_entry"
    data = result["data"]
    assert data[CONF_MODE] == "observe"
    for key, value in ALARM_OPTIONS.items():
        assert data[key] == value, f"{key} was dropped"


async def test_toggling_speech_memory_keeps_every_alarm_option(hass: FakeHass) -> None:
    entry = await set_up(hass)
    result = await options_flow(hass, entry).async_step_speech_memory_toggle(
        {CONF_SPEECH_MEMORY: True}
    )

    assert result["type"] == "create_entry"
    data = result["data"]
    assert data[CONF_SPEECH_MEMORY] is True
    for key, value in ALARM_OPTIONS.items():
        assert data[key] == value, f"{key} was dropped"


async def test_alarm_page_shows_the_stored_values(hass: FakeHass) -> None:
    entry = await set_up(
        hass, {CONF_MODE: "active", **ALARM_OPTIONS, CONF_ALARM_SATELLITE: SATELLITE}
    )
    result = await options_flow(hass, entry).async_step_alarms()

    assert result["step_id"] == "alarms"
    assert schema_marker(result, CONF_ALARM_SATELLITE).description == {"suggested_value": SATELLITE}
    assert schema_marker(result, CONF_ALARM_RING_SECONDS).default() == 60
    assert schema_marker(result, CONF_ALARM_VOLUME).default() == 0.3
    assert schema_marker(result, CONF_ALARM_MEDIA).default() == ALARM_OPTIONS[CONF_ALARM_MEDIA]
    assert result["description_placeholders"]["targets"] == TARGET


async def test_alarm_page_stores_satellite_and_music(hass: FakeHass) -> None:
    entry = await set_up(hass)
    result = await options_flow(hass, entry).async_step_alarms(
        {
            CONF_ALARM_SATELLITE: SATELLITE,
            CONF_ALARM_MUSIC: TARGET,
            CONF_ALARM_RING_SECONDS: 45,
            CONF_ALARM_VOLUME: 0.5,
            CONF_ALARM_MEDIA: "",
            CONF_ALARM_MEDIA_TYPE: "",
        }
    )

    assert result["type"] == "create_entry"
    data = result["data"]
    assert data[CONF_ALARM_SATELLITE] == SATELLITE
    assert data[CONF_ALARM_MUSIC] == TARGET
    assert data[CONF_ALARM_RING_SECONDS] == 45
    assert data[CONF_ALARM_VOLUME] == 0.5
    # An empty field must not wipe a configured alarm file.
    assert data[CONF_ALARM_MEDIA] == ALARM_OPTIONS[CONF_ALARM_MEDIA]
    assert data[CONF_ALARM_MEDIA_TYPE] == ALARM_OPTIONS[CONF_ALARM_MEDIA_TYPE]
    assert data[CONF_ALARM_DB] == ALARM_OPTIONS[CONF_ALARM_DB]
    assert data[CONF_ALARM_PILOT] == ALARM_OPTIONS[CONF_ALARM_PILOT]


async def test_alarm_page_can_clear_the_satellite(hass: FakeHass) -> None:
    entry = await set_up(
        hass, {CONF_MODE: "active", **ALARM_OPTIONS, CONF_ALARM_SATELLITE: SATELLITE}
    )
    result = await options_flow(hass, entry).async_step_alarms(
        {CONF_ALARM_SATELLITE: "", CONF_ALARM_MUSIC: ""}
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_ALARM_SATELLITE] is None
    assert result["data"][CONF_ALARM_MUSIC] is None


async def test_music_player_outside_the_targets_is_rejected(hass: FakeHass) -> None:
    entry = await set_up(hass)
    before = dict(entry.options)
    result = await options_flow(hass, entry).async_step_alarms(
        {CONF_ALARM_MUSIC: "media_player.not_allowed"}
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "music_not_a_target"}
    assert entry.options == before


async def test_alarm_page_without_alarm_database_aborts(hass: FakeHass) -> None:
    entry = await set_up(hass, {CONF_MODE: "active"})
    result = await options_flow(hass, entry).async_step_alarms()

    assert result["type"] == "abort"
    assert result["reason"] == "alarms_not_configured"


async def test_saved_satellite_reaches_the_alarm_layer(tmp_path) -> None:
    # The point of the option: without it the session path does not exist.
    class Config:
        time_zone = "Europe/Berlin"

        def path(self, name):
            return str(tmp_path / name)

    class Hass:
        config = Config()

    without, _ = build_alarm_layer(Hass(), {**ALARM_OPTIONS})
    with_satellite, _ = build_alarm_layer(
        Hass(), {**ALARM_OPTIONS, CONF_ALARM_SATELLITE: SATELLITE}
    )

    assert without.scheduler.session_factory is None
    assert with_satellite.scheduler.session_factory is not None


@pytest.mark.parametrize("key", ["alarm_satellite", "alarm_music_entity"])
def test_alarm_keys_are_only_defined_once(key: str) -> None:
    # A typo here would silently disable an option again, so both spellings are
    # asserted literally instead of relying on the constants.
    assert key in {
        config_flow.CONF_ALARM_SATELLITE,
        config_flow.CONF_ALARM_MUSIC,
    }
