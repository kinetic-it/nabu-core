"""Service descriptions for the Nabu Core alarm domain.

The alarm services are registered by the kinetic_voice_router integration when
the alarm_db option is set. This integration only carries the descriptions so
Home Assistant can render the service fields and does not report an unknown
domain. It registers no services and stores no state.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Nothing to set up: the services belong to the router integration."""
    return True
