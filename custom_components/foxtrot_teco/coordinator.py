from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import timedelta


from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import TecoApiClient
from .const import (
    DOMAIN,
    DEFAULT_SCAN_INTERVAL,
    HA_NESTED_PATTERN,
    FACE_FIELDS_POLL,
)

_LOGGER = logging.getLogger(__name__)
RE_HA = re.compile(HA_NESTED_PATTERN)


@dataclass
class Face:
    """A discovered HA face exposed by an FB: <prefix>.HA.<leaf>."""
    key: str                    # prefix (e.g., "livingroom", "Rooms[1]")
    fields: Dict[str, str]      # leaf -> full object name (e.g., "pv" -> "livingroom.HA.pv")


class FoxtrotCoordinator(DataUpdateCoordinator):
    """Keeps a local cache of all discovered faces & their values."""

    def __init__(self, hass: HomeAssistant, api: TecoApiClient, scan_seconds: int):
        super().__init__(hass, _LOGGER, name="Foxtrot TECO")
        self.api = api
        self.update_interval = timedelta(seconds=scan_seconds or DEFAULT_SCAN_INTERVAL)
        self.faces: Dict[str, Face] = {}      # key -> Face
        self.data: Dict[str, object] = {}     # object full name -> value

    async def discover(self) -> None:
        try:
            objs = await self.api.get_list()
        except Exception as e:
            _LOGGER.warning("Discover: GetList failed (will retry on next update): %s", e)
            self.faces = {}
            return
        buckets: Dict[str, Dict[str, str]] = {}
        for obj in objs:
            m = RE_HA.match(obj)
            if not m:
                continue
            key = m.group("prefix")
            leaf = m.group("leaf")
            buckets.setdefault(key, {})[leaf] = obj

        self.faces = {k: Face(k, v) for k, v in buckets.items()}
        _LOGGER.info("Discovered %d HA faces", len(self.faces))

    async def _async_update_data(self):
        names: List[str] = []
        # collect all known fields we care about; missing ones are ignored
        for f in self.faces.values():
            for leaf in FACE_FIELDS_POLL:
                n = f.fields.get(leaf)
                if n:
                    names.append(n)
        if not names:
            return {}
        values = await self.api.get_many(list(set(names)))
        self.data.update(values)
        return self.data

    def face_value(self, face: Face, leaf: str, default=None):
        obj = face.fields.get(leaf)
        if not obj:
            return default
        return self.data.get(obj, default)
