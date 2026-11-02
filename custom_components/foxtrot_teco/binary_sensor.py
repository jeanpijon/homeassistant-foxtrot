from __future__ import annotations

from typing import Any
import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from . import make_unique_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator = store["coordinator"]
    client = store["client"]

    entities: list[TecoBinarySensor] = []
    data = coordinator.data or {}
    if not data:
        _LOGGER.warning("TecoAPI binary_sensor: coordinator.data is empty at setup")

    for var_path, face in data.items():
        # expect faces shaped like:
        # {"name": "...", "kind": 3, "writable": true/false, "state": bool, "key": "...", "_serial": "...", "device_class": "...?"}
        try:
            kind = int(face.get("kind", 0))
        except Exception as exc:
            continue
        # Binary sensors are boolean faces that are NOT writable
        if kind == 3 and not bool(face.get("writable", False)):
            entities.append(TecoBinarySensor(coordinator, client, var_path))



    if entities:
        async_add_entities(entities)


class TecoBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Read-only boolean from Foxtrot via TecoAPI."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator, client, var_path: str) -> None:
        super().__init__(coordinator)
        self._client = client
        self._path = var_path
        self._update_from_face()

    # ----- helpers ---------------------------------------------------------
    def _face(self) -> dict[str, Any]:
        # coordinator.data is the single source of truth
        return self.coordinator.data[self._path]

    def _update_from_face(self) -> None:
        face = self._face()

        # Name
        name = face.get("name") or self._path
        self._attr_name = str(name)

        # Identity
        serial = str(face.get("_serial", "unknown"))
        key = face.get("key")
        kind = int(face.get("kind", 3))
        self._attr_unique_id = make_unique_id(serial, kind, key, self._path)

        # Optional device_class if your backend provides it
        # (e.g., "door", "window", "motion", "problem", ...)
        self._attr_device_class = face.get("device_class") or None

        # Group all entities under the PLC device
        self._attr_device_info = {
            "identifiers": {(DOMAIN, serial)},
            "manufacturer": "TECO",
            "name": f"Foxtrot {serial}",
        }

    # ----- entity API ------------------------------------------------------
    @property
    def available(self) -> bool:
        # present if path exists in the latest coordinator snapshot
        return self._path in self.coordinator.data

    @property
    def is_on(self) -> bool:
        # server provides boolean; be tolerant of 0/1
        face = self._face()
        val = face.get("state", False)
        if isinstance(val, (int, float)):
            return float(val) >= 0.5
        return bool(val)

    # ----- coordinator hook ------------------------------------------------
    def _handle_coordinator_update(self) -> None:
        # refresh attributes (name/device_class could change) and state
        try:
            self._update_from_face()
        except KeyError:
            # face disappeared; let HA mark it unavailable on write
            pass
        self.async_write_ha_state()
