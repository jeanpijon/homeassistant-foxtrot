from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, UNIT2DEVICE_CLASS
from . import make_unique_id


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator = store["coordinator"]

    entities: list[TecoSensor] = []
    for var_path, face in coordinator.data.items():
        # kind==2 => sensor
        try:
            if int(face.get("kind", 0)) != 2:
                continue
        except Exception:
            continue
        entities.append(TecoSensor(coordinator, var_path))

    if entities:
        async_add_entities(entities)


class TecoSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, var_path: str):
        super().__init__(coordinator)
        self._path = var_path
        self._update_from_face()

    def _face(self) -> dict:
        return self.coordinator.data[self._path]

    def _update_from_face(self) -> None:
        face = self._face()
        self._attr_name = face.get("name") or self._path
        device_class, native_unit, precision = UNIT2DEVICE_CLASS[face.get("unit", "0")]
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = native_unit
        self._attr_suggested_display_precision = precision
        serial = face.get("_serial", "unknown")
        key = face.get("key")
        kind = int(face.get("kind", 2))
        self._attr_unique_id = make_unique_id(serial, kind, key, self._path)

        # Group under the PLC device
        self._attr_device_info = {
            "identifiers": {(DOMAIN, serial)},
            "manufacturer": "TECO",
            "name": f"Foxtrot {serial}",
        }

    @property
    def native_value(self):
        return float(self._face().get("value"))

    def _handle_coordinator_update(self) -> None:
        self._update_from_face()
        self.async_write_ha_state()
