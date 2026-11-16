from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    return {
        "entry": async_redact_data(dict(entry.data), {"host"}),
        "connected": coordinator.client.connected,
        "types": coordinator.client.types,
        "data": coordinator.data,
    }
