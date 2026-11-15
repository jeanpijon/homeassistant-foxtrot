from __future__ import annotations
import hashlib
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, PLATFORMS
from .api import TecoApiClient

_LOGGER = logging.getLogger(__name__)


def make_unique_id(serial: str, kind: int, key: str | None, path: str) -> str:
    base = f"{serial}|{kind}|{key or path}".encode()
    return "teco_" + hashlib.sha1(base).hexdigest()[:20]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    data = entry.data
    host = data.get("host") or data.get("resource")
    if not host:
        _LOGGER.error("Missing host/resource in config entry")
        return False

    session = async_get_clientsession(hass)
    client = TecoApiClient(
        host,
        session,
        data.get("username"),
        data.get("password"),
        data.get("auth_type", "digest"),
        data.get("verify_ssl", True),
    )

    async def _update():
        try:
            info = await client.get_info()
            serial = info.get("plc", {}).get("serialNumber")
            if not serial:
                # The serial is part of every entity's unique id and of the device identity:
                # entities created under a placeholder would be duplicated after the next restart.
                raise UpdateFailed("GetInfo has no PLC serial number yet (PLC still starting?)")
            names = await client.get_list(prefix="ha_")
            if not names:
                # The HTTP server can answer before the PLC program runs. Entities are created
                # from this first answer, so an empty one would mean no entities until a reload.
                raise UpdateFailed("TecoApi lists no ha_* objects yet (PLC program not running?)")
            out = {}
            for name in names:
                obj = await client.get_object(name)
                face = obj.get(name, {})
                face["_serial"] = serial
                face["_var_path"] = name
                out[name] = face
            return out
        except Exception as e:
            raise UpdateFailed(str(e)) from e

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=f"{DOMAIN}_coordinator",
        update_method=_update,
        update_interval=timedelta(seconds=5),
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
