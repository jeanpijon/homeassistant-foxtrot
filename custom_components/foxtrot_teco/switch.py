from __future__ import annotations

from typing import Any
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from . import make_unique_id


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator = store["coordinator"]
    client = store["client"]

    entities: list[TecoSwitch] = []
    for var_path, face in coordinator.data.items():
        # expect faces shaped like:
        # {"name": "...", "kind": 3, "writable": true/false, "state": bool, "key": "...", "_serial": "..."}
        try:
            kind = int(face.get("kind", 0))
        except Exception:
            continue
        if kind == 3 and bool(face.get("writable", False)):
            entities.append(TecoSwitch(coordinator, client, var_path))

    if entities:
        async_add_entities(entities)


class TecoSwitch(CoordinatorEntity, SwitchEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator, client, var_path: str):
        super().__init__(coordinator)
        self._client = client
        self._path = var_path
        self._update_from_face()

    def _face(self) -> dict[str, Any]:
        return self.coordinator.data[self._path]

    def _update_from_face(self) -> None:
        face = self._face()
        name = face.get("name") or self._path
        self._attr_name = str(name)

        serial = face.get("_serial", "unknown")
        key = face.get("key")
        kind = int(face.get("kind", 3))
        self._attr_unique_id = make_unique_id(serial, kind, key, self._path)

        # group all entities under the PLC device
        self._attr_device_info = {
            "identifiers": {(DOMAIN, serial)},
            "manufacturer": "TECO",
            "name": f"Foxtrot {serial}",
        }

    @property
    def available(self) -> bool:
        # the link is up and the face is present
        return super().available and self._path in self.coordinator.data

    @property
    def is_on(self) -> bool:
        face = self._face()
        # server provides boolean; be tolerant of 0/1
        val = face.get("state", False)
        if isinstance(val, (int, float)):
            return float(val) >= 0.5
        return bool(val)

    async def async_turn_on(self, **kwargs):
        # The PLC confirms with a push, which updates the entity.
        await self.coordinator.async_write(self._path, "state", True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_write(self._path, "state", False)

    def _handle_coordinator_update(self) -> None:
        self._update_from_face()
        self.async_write_ha_state()
