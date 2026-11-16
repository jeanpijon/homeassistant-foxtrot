from __future__ import annotations
from typing import Any, Dict
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_SERIAL, DOMAIN, HA_PREFIX
from .plccoms import DEFAULT_PORT, PlccomsClient, PlccomsError


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
