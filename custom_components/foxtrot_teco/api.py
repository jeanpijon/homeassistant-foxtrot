from __future__ import annotations

import json

from aiohttp import ClientSession, ClientResponseError, BasicAuth
from typing import Any, Dict, List, Optional
import logging

_LOGGER = logging.getLogger(__name__)
try:
    from aiohttp import DigestAuth  # aiohttp >= 3.8
except Exception:
    DigestAuth = None  # type: ignore


class TecoApiClient:
    def __init__(
        self,
        host: str,
        session: ClientSession,
        username: Optional[str] = None,
        password: Optional[str] = None,
        auth_type: str = "digest",      # "digest" | "basic" | "auto"
        verify_ssl: bool = True,
    ):
        self._host = host.rstrip("/")
        self._session = session
        self._username = username
        self._password = password
        self._auth_type = (auth_type or "digest").lower()
        self._verify_ssl = verify_ssl

    def _auth_digest(self):
        if not self._username or DigestAuth is None:
            return None
        return DigestAuth(self._username, self._password or "")

    def _auth_basic(self):
        if not self._username:
            return None
        return BasicAuth(self._username, self._password or "")

    async def _request_json(self, path: str, auth, **params) -> Dict[str, Any]:
        url = f"{self._host}/TecoApi/{path}"
        async with self._session.get(url, params=params, auth=auth, ssl=self._verify_ssl) as resp:
            if resp.status == 401:
                wa = resp.headers.get("WWW-Authenticate", "")
                _LOGGER.debug("401 from %s, WWW-Authenticate: %s", url, wa)
            resp.raise_for_status()
            return await resp.json()

    async def _get_json(self, path: str, **params) -> Dict[str, Any]:
        if self._auth_type == "basic":
            primary, fallback = self._auth_basic(), self._auth_digest()
        elif self._auth_type == "auto":
            primary, fallback = self._auth_digest(), self._auth_basic()
        else:  # digest default
            primary, fallback = self._auth_digest(), self._auth_basic()

        try:
            return await self._request_json(path, primary, **params)
        except ClientResponseError as e:
            if e.status == 401 and fallback is not None:
                return await self._request_json(path, fallback, **params)
            raise

    async def get_info(self) -> Dict[str, Any]:
        return await self._get_json("GetInfo")

    async def get_list(self, prefix: str = "ha_") -> List[str]:
        data = await self._get_json("GetList")
        return [k for k,v in data.items() if k.startswith(prefix)]

    async def get_object(self, var_name: str) -> Dict[str, Any]:
        return await self._get_json("GetObject", **{var_name: ""})

    async def set_object(self, var_name: str, payload: dict) -> dict:
        """
        Simplified writer for Foxtrot TECO:
        /TecoApi/SetObject?<var>.<field>=<value>
        e.g.  SetObject?ha_livingroom_circuit1.state=false
        """
        # select auth pair (digest first)
        primary, fallback = self._select_auth_pair()

        if len(payload) != 1:
            raise ValueError("set_object() expects exactly one field/value pair")

        (field, value), = payload.items()

        # normalise bools → "true"/"false", numbers → str
        if isinstance(value, bool):
            vstr = "true" if value else "false"
        elif isinstance(value, (int, float)):
            vstr = str(value)
        else:
            # anything else, JSON-encode
            import json
            vstr = json.dumps(value, separators=(",", ":"))

        params = {f"{var_name}.{field}": vstr}
        url = f"{self._host}/TecoApi/SetObject"

        for auth in (primary, fallback):
            if auth is None:
                continue
            async with self._session.get(url, params=params, auth=auth, ssl=self._verify_ssl) as resp:
                if resp.status == 401:
                    # try fallback auth next loop
                    continue
                resp.raise_for_status()
                try:
                    return await resp.json()
                except Exception:
                    return {}

        raise RuntimeError(f"SetObject failed for {var_name}.{field}={vstr}")

    def _select_auth_pair(self):
        if self._auth_type == "basic":
            return self._auth_basic(), self._auth_digest()
        elif self._auth_type == "auto":
            return self._auth_digest(), self._auth_basic()
        else:  # digest default
            return self._auth_digest(), self._auth_basic()
