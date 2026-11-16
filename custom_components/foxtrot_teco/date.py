"""The holiday period of a thermostat's weekly program (the mode is eco during it)."""
from __future__ import annotations

from dataclasses import replace
from datetime import date

from homeassistant.components.date import DateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import make_unique_id
from .const import DOMAIN

UNSET_YEAR = 1971          # the PLC's "no date" is 1970-01-01


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        TecoHoliday(coordinator, path, end)
        for path, face in coordinator.data.items()
        if face.get("_tpw")
        for end in (0, 1)
    )


class TecoHoliday(CoordinatorEntity, DateEntity):
    """First (0) or last (1) day of the holiday: the PLC runs eco in the room from the first to the last day."""

    _attr_has_entity_name = True
    _attr_translation_key = "holiday"

    def __init__(self, coordinator, var_path: str, end: int):
        super().__init__(coordinator)
        self._path, self._end = var_path, end
        face = coordinator.data[var_path]
        self._prefix = face["_tpw"]["_prefix"]
        self._attr_translation_placeholders = {"room": face["_tpw"].get("name") or var_path, "end": ("from", "to")[end]}
        serial, key = face.get("_serial", "unknown"), face.get("key")
        self._attr_unique_id = make_unique_id(serial, int(face.get("kind", 1)), f"{key or var_path}:holiday:{end}", var_path)
        self._attr_device_info = {"identifiers": {(DOMAIN, serial)}, "manufacturer": "TECO", "name": f"Foxtrot {serial}"}

    @property
    def available(self) -> bool:
        return super().available and self._prefix in self.coordinator.programs

    @property
    def native_value(self) -> date | None:
        program = self.coordinator.programs.get(self._prefix)
        if not program:
            return None
        try:
            day = date.fromisoformat(program.holiday[self._end][:10])
        except ValueError:
            return None
        return day if day.year >= UNSET_YEAR else None

    async def async_set_value(self, value: date) -> None:
        program = self.coordinator.programs[self._prefix]
        holiday = list(program.holiday)
        holiday[self._end] = f"{value:%Y-%m-%d}-00:00:0"
        await self.coordinator.async_write_program(self._prefix, replace(program, holiday=tuple(holiday)))
