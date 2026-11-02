from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN

async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "host": coordinator.host,
        "username": coordinator.port,
        "last_data": coordinator.data,
    }
