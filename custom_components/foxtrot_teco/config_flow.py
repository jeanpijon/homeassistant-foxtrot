from __future__ import annotations
from typing import Any, Dict
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    EntitySelector, EntitySelectorConfig, SelectOptionDict, SelectSelector, SelectSelectorConfig,
)

from .const import CONF_SCHEDULES, CONF_SERIAL, DOMAIN, HA_PREFIX
from .plccoms import DEFAULT_PORT, PlccomsClient, PlccomsError
from .thermostat import MODE_PRESETS


def _schema(host: str = "192.168.11.11", port: int = DEFAULT_PORT) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_HOST, default=host): str,
        vol.Required(CONF_PORT, default=port): int,
    })


class FoxtrotTecoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        errors: Dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            client = PlccomsClient(host, port)
            title = "Foxtrot TECO"
            try:
                await client.connect()
                title = f"Foxtrot TECO ({' '.join((await client.get_info('version_plc')).split())})"
                if not await client.list_variables(f"{HA_PREFIX}*"):
                    errors["base"] = "no_ha_objects"
            except PlccomsError:
                errors["base"] = "cannot_connect"
            finally:
                await client.close()

            if not errors:
                await self.async_set_unique_id(f"{DOMAIN}:{host}:{port}")
                self._abort_if_unique_id_configured()
                # The serial number is part of the unique id of every entity. TecoApi could tell it;
                # PLCComS cannot, so a new installation is identified by its address.
                return self.async_create_entry(
                    title=title, data={CONF_HOST: host, CONF_PORT: port, CONF_SERIAL: host}
                )
            return self.async_show_form(step_id="user", data_schema=_schema(host, port), errors=errors)

        return self.async_show_form(step_id="user", data_schema=_schema(), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "FoxtrotOptionsFlow":
        return FoxtrotOptionsFlow()


class FoxtrotOptionsFlow(config_entries.OptionsFlow):
    """Link a thermostat's weekly program to a schedule helper (or unlink it), one thermostat at a time."""

    def __init__(self) -> None:
        self._room: str | None = None

    def _rooms(self) -> list[str]:
        store = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if not store:
            return []
        return sorted({face["_tpw"]["name"] for face in store["coordinator"].data.values()
                       if face.get("_tpw") and face["_tpw"].get("name")})

    async def async_step_init(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        rooms = self._rooms()
        if not rooms:
            return self.async_abort(reason="no_thermostats")
        links = self.config_entry.options.get(CONF_SCHEDULES) or {}
        if user_input is not None:
            self._room = user_input["room"]
            return await self.async_step_room()
        options = [SelectOptionDict(value=room, label=f"{room}  ({links[room]['entity_id']})" if room in links else room)
                   for room in rooms]
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required("room"): SelectSelector(SelectSelectorConfig(options=options)),
        }))

    async def async_step_room(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        room = self._room
        options = dict(self.config_entry.options)
        links = dict(options.get(CONF_SCHEDULES) or {})
        current = links.get(room, {})
        errors: Dict[str, str] = {}
        if user_input is not None:
            entity = user_input.get("schedule")
            if not entity:
                links.pop(room, None)
            elif entity != current.get("entity_id") and not user_input.get("confirm"):
                errors["confirm"] = "confirm_overwrite"
            else:
                links[room] = {"entity_id": entity, "base_mode": user_input["base_mode"]}
            if not errors:
                return self.async_create_entry(data={**options, CONF_SCHEDULES: links})
        schema = vol.Schema({
            vol.Optional("schedule", description={"suggested_value": current.get("entity_id")}):
                EntitySelector(EntitySelectorConfig(domain="schedule")),
            vol.Required("base_mode", default=current.get("base_mode", "eco")):
                SelectSelector(SelectSelectorConfig(options=list(MODE_PRESETS.values()), translation_key="base_mode")),
            vol.Optional("confirm", default=False): bool,
        })
        return self.async_show_form(step_id="room", data_schema=schema, errors=errors,
                                    description_placeholders={"room": room})
