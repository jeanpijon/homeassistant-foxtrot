from __future__ import annotations
from typing import Any, Dict
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD, CONF_VERIFY_SSL
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, CONF_RESOURCE, CONF_AUTH_TYPE, AUTH_DIGEST, AUTH_BASIC, AUTH_AUTO, DEFAULT_VERIFY_SSL
from .api import TecoApiClient


class FoxtrotTecoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        errors: Dict[str, str] = {}

        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({
                    vol.Required(CONF_RESOURCE, default="http://192.168.11.11"): str,
                    vol.Optional(CONF_USERNAME): str,
                    vol.Optional(CONF_PASSWORD): str,
                    vol.Optional(CONF_AUTH_TYPE, default=AUTH_DIGEST): vol.In([AUTH_DIGEST, AUTH_BASIC, AUTH_AUTO]),
                    vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
                }),
                errors=errors,
            )

        # validate by calling GetInfo
        session = async_get_clientsession(self.hass)
        client = TecoApiClient(
            user_input[CONF_RESOURCE],
            session,
            user_input.get(CONF_USERNAME),
            user_input.get(CONF_PASSWORD),
            user_input.get(CONF_AUTH_TYPE, AUTH_DIGEST),
            user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        )
        try:
            info = await client.get_info()
        except Exception:
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({
                    vol.Required(CONF_RESOURCE, default=user_input[CONF_RESOURCE]): str,
                    vol.Optional(CONF_USERNAME, default=user_input.get(CONF_USERNAME, "")): str,
                    vol.Optional(CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, "")): str,
                    vol.Optional(CONF_AUTH_TYPE, default=user_input.get(CONF_AUTH_TYPE, AUTH_DIGEST)): vol.In([AUTH_DIGEST, AUTH_BASIC, AUTH_AUTO]),
                    vol.Optional(CONF_VERIFY_SSL, default=user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)): bool,
                }),
                errors=errors,
            )

        plc = info.get("plc", {})
        title = f"Foxtrot TECO ({plc.get('type','?')})"
        # store normalized keys
        data = {
            "host": user_input[CONF_RESOURCE],
            "username": user_input.get(CONF_USERNAME),
            "password": user_input.get(CONF_PASSWORD),
            "auth_type": user_input.get(CONF_AUTH_TYPE, AUTH_DIGEST),
            "verify_ssl": user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        }
        # unique entry per host
        await self.async_set_unique_id(f"{DOMAIN}:{data['host']}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=title, data=data)
