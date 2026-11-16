"""The temperatures of a thermostat's four modes, from its weekly program."""
from __future__ import annotations

from dataclasses import replace

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import make_unique_id
from .const import DOMAIN
from .thermostat import MODE_PRESETS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        TecoModeTemperature(coordinator, path, mode)
        for path, face in coordinator.data.items()
        if face.get("_tpw")
        for mode in MODE_PRESETS
    )


class TecoModeTemperature(CoordinatorEntity, NumberEntity):
    """The heating temperature of one mode (limit, eco, sleep, comfort) of a room's weekly program."""

    _attr_has_entity_name = True
    _attr_translation_key = "mode_temperature"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 5.0
    _attr_native_max_value = 35.0
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, var_path: str, mode: int):
        super().__init__(coordinator)
        self._path, self._mode = var_path, mode
        face = coordinator.data[var_path]
        self._prefix = face["_tpw"]["_prefix"]
        self._attr_translation_placeholders = {"room": face["_tpw"].get("name") or var_path,
                                               "mode": MODE_PRESETS[mode]}
        serial, key = face.get("_serial", "unknown"), face.get("key")
        self._attr_unique_id = make_unique_id(serial, int(face.get("kind", 1)), f"{key or var_path}:heat:{mode}", var_path)
        self._attr_device_info = {"identifiers": {(DOMAIN, serial)}, "manufacturer": "TECO", "name": f"Foxtrot {serial}"}

    @property
    def available(self) -> bool:
        return super().available and self._prefix in self.coordinator.programs

    @property
    def native_value(self) -> float | None:
        program = self.coordinator.programs.get(self._prefix)
        return program.heat_temp[self._mode] if program else None

    async def async_set_native_value(self, value: float) -> None:
        program = self.coordinator.programs[self._prefix]
        temps = list(program.heat_temp)
        temps[self._mode] = float(value)
        await self.coordinator.async_write_program(self._prefix, replace(program, heat_temp=tuple(temps)))
