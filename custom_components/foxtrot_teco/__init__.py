from __future__ import annotations
import hashlib
import logging
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_SERIAL, DOMAIN, PLATFORMS
from .coordinator import FoxtrotCoordinator
from .plccoms import DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)


def make_unique_id(serial: str, kind: int, key: str | None, path: str) -> str:
    base = f"{serial}|{kind}|{key or path}".encode()
    return "teco_" + hashlib.sha1(base).hexdigest()[:20]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    coordinator = FoxtrotCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        # Not ready (PLC down, program not running): HA retries; don't leave the connection open.
        await coordinator.async_shutdown()
        raise
    coordinator.start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": coordinator.client,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        store = hass.data[DOMAIN].pop(entry.entry_id, None)
        if store:
            await store["coordinator"].async_shutdown()
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1.x talked to TecoApi over HTTP (URL, login); 2.x talks to PLCComS (host, port).

    Entity unique ids hash the PLC serial number, which only TecoApi could tell; 2.x takes it over
    from the device that 1.x registered, so entities, history and the device stay as they are.
    """
    if entry.version == 1:
        old = entry.data.get("host") or entry.data.get("resource") or ""
        host = urlparse(old if "://" in old else f"//{old}").hostname
        if not host:
            _LOGGER.error("Cannot migrate the Foxtrot entry: no host in %r", old)
            return False
        registry = dr.async_get(hass)
        serials = sorted(
            ident
            for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
            for domain, ident in device.identifiers
            if domain == DOMAIN and ident != "unknown"
        )
        serial = serials[0] if serials else host
        hass.config_entries.async_update_entry(
            entry,
            data={"host": host, "port": DEFAULT_PORT, CONF_SERIAL: serial},
            unique_id=f"{DOMAIN}:{host}:{DEFAULT_PORT}",
            version=2,
        )
        _LOGGER.info("Migrated the Foxtrot entry to PLCComS at %s:%d (serial %s)", host, DEFAULT_PORT, serial)
    return True
