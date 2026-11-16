from __future__ import annotations

from typing import Any
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode, HVACAction,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.const import UnitOfTemperature

from .const import DOMAIN
from . import make_unique_id


ACTION_TO_HVAC = {
    0: HVACAction.IDLE,
    1: HVACAction.HEATING,
    2: HVACAction.COOLING
}
HVAC_TO_ACTION = {v: k for k, v in ACTION_TO_HVAC.items()}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator = store["coordinator"]
    client = store["client"]

    entities: list[TecoClimate] = []
    for var_path, face in coordinator.data.items():
        try:
            if int(face.get("kind", 0)) != 1:
                continue
        except Exception:
            continue
        entities.append(TecoClimate(coordinator, client, var_path))

    if entities:
        async_add_entities(entities)


class TecoClimate(CoordinatorEntity, ClimateEntity):
    _attr_has_entity_name = True
    # Home Assistant 2026.11 renamed the climate unit attribute; older releases only know the old one.
    if hasattr(ClimateEntity, "native_temperature_unit"):
        _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    else:
        _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, client, var_path: str):
        super().__init__(coordinator)
        self._client = client
        self._path = var_path
        self._supported = ClimateEntityFeature(0)
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO]
        self._attr_hvac_mode = HVACMode.AUTO
        self._update_from_face()

    def _face(self) -> dict[str, Any]:
        return self.coordinator.data[self._path]

    def _update_from_face(self) -> None:
        f = self._face()
        self._attr_name = f.get("name") or self._path
        self._attr_current_temperature = f.get("pv")
        self._attr_target_temperature = f.get("sp")

        # Features based on writables
        self._supported = ClimateEntityFeature(0)
        if f.get("writable_sp", True):
            self._supported |= ClimateEntityFeature.TARGET_TEMPERATURE
        if f.get("writable_mode", True):
            # hvac modes already exposed; nothing to add here
            pass
        self._attr_supported_features = self._supported

        # hvac mode mapping
        action_int = int(f.get("mode", 0))  # default idle
        self._attr_hvac_action = ACTION_TO_HVAC.get(action_int, HVACAction.IDLE)

        # identity
        serial = f.get("_serial", "unknown")
        key = f.get("key")
        kind = int(f.get("kind", 1))
        self._attr_unique_id = make_unique_id(serial, kind, key, self._path)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, serial)},
            "manufacturer": "TECO",
            "name": f"Foxtrot {serial}",
        }

    def _handle_coordinator_update(self) -> None:
        self._update_from_face()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs) -> None:
        if ClimateEntityFeature.TARGET_TEMPERATURE not in self._supported:
            return
        temp = kwargs.get("temperature")
        if temp is None:
            return
        # The PLC confirms with a push, which updates the entity.
        await self.coordinator.async_write(self._path, "sp", float(temp))
