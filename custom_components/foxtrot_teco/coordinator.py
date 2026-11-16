"""Mirrors the PLC's ha_* objects, pushed by PLCComS, and keeps the link up."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import const
from .const import CONF_SERIAL, DOMAIN, HA_PREFIX
from .plccoms import PLC_RUN, PlccomsClient, PlccomsError

_LOGGER = logging.getLogger(__name__)

FIELD_RE = re.compile(rf"^(?P<obj>{re.escape(HA_PREFIX)}[^.]+)\.(?P<field>\w+)$")


def faces_from_values(values: dict[str, Any], serial: str) -> dict[str, dict[str, Any]]:
    """Group ``ha_x.field`` variables into one face per object, as {var_path: {field: value}}.

    An object without a ``kind`` field cannot be told apart (sensor, switch, climate) and is skipped.
    """
    faces: dict[str, dict[str, Any]] = {}
    for name, value in values.items():
        match = FIELD_RE.match(name)
        if match:
            faces.setdefault(match["obj"], {})[match["field"]] = value
    faces = {path: face for path, face in faces.items() if "kind" in face}
    for path, face in faces.items():
        face["_serial"] = serial
        face["_var_path"] = path
    return faces


class FoxtrotCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """``data`` is {var_path: face}; it is replaced whenever the PLC pushes a change."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=None)
        self.serial: str = entry.data[CONF_SERIAL]
        self.client = PlccomsClient(
            entry.data["host"],
            entry.data["port"],
            on_change=self._on_change,
            on_reload=self._wake_up,
            on_disconnect=self._wake_up,
        )
        self._ready = False                      # pushes are ignored until the first refresh is done
        self._objects: frozenset[str] = frozenset()
        self._reloading = False
        self._synced_at = 0.0
        self._attention = asyncio.Event()        # something needs the supervisor's look now
        self._flush_handle: asyncio.TimerHandle | None = None
        self._task: asyncio.Task | None = None

    # ----- first refresh and supervision -------------------------------------------------

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        return await self._sync()

    def start(self) -> None:
        """Called once the first refresh succeeded: from now on the PLC pushes."""
        self._ready = True
        self._task = self.config_entry.async_create_background_task(
            self.hass, self._supervise(), f"{DOMAIN} link to {self.client.host}"
        )

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self.client.close()

    async def _sync(self) -> dict[str, dict[str, Any]]:
        """(Re)connect, read everything, and have the PLC push changes from now on."""
        try:
            await self.client.connect()
            return await self._load()
        except Exception as err:
            await self.client.close()
            if isinstance(err, UpdateFailed):
                raise
            raise UpdateFailed(str(err)) from err

    async def _load(self) -> dict[str, dict[str, Any]]:
        client = self.client
        variables = await client.list_variables(f"{HA_PREFIX}*")
        if not variables:
            # PLCComS can answer before the PLC program runs, or serve a public file without them.
            raise UpdateFailed(f"PLCComS lists no {HA_PREFIX}* variables (PLC program not running, or "
                               f"not in the public file it serves)")
        if not await client.ping():
            raise UpdateFailed("the PLC program is not running")

        for name, plc_type in variables.items():
            real = plc_type.upper() in ("REAL", "LREAL")
            await client.enable(name, const.REAL_DELTA if real else None)
        await client.enable(PLC_RUN)
        answered = set(await client.snapshot(f"{HA_PREFIX}*"))
        if missing := set(variables) - answered:
            raise UpdateFailed(f"PLCComS returned {len(answered)} of {len(variables)} variables "
                               f"(not enabled: {', '.join(sorted(missing)[:3])}...)")

        faces = faces_from_values(client.values, self.serial)
        skipped = {m["obj"] for name in variables if (m := FIELD_RE.match(name))} - set(faces)
        if skipped:
            _LOGGER.warning("Ignoring PLC objects without a 'kind' field: %s", ", ".join(sorted(skipped)))
        if not faces:
            raise UpdateFailed(f"no usable {HA_PREFIX}* objects")
        self._synced_at = time.monotonic()
        self._note_objects(faces)
        return faces

    def _note_objects(self, faces: dict[str, dict[str, Any]]) -> None:
        """Entities are created once; a changed set of objects needs a reload to show up."""
        objects = frozenset(faces)
        if not self._objects:
            self._objects = objects
        elif objects != self._objects and not self._reloading:
            _LOGGER.info("The PLC program now publishes different objects (%d, was %d): reloading",
                         len(objects), len(self._objects))
            self._reloading = True
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    async def _supervise(self) -> None:
        delay = const.RECONNECT_MIN
        while True:
            try:
                if self.last_update_success and self.client.connected:
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(const.PING_INTERVAL):
                            await self._attention.wait()
                    await self._check()
                else:
                    self._attention.clear()
                    faces = await self._sync()
                    self.async_set_updated_data(faces)
                    _LOGGER.info("Connected to the PLC at %s: %d objects", self.client.host, len(faces))
                    delay = const.RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except Exception as err:
                await self.client.close()
                _LOGGER.debug("PLC link down: %s (retry in %s s)", err, delay)
                self.async_set_update_error(err if isinstance(err, UpdateFailed) else UpdateFailed(str(err)))
                await asyncio.sleep(delay)
                delay = min(delay * 2, const.RECONNECT_MAX)

    async def _check(self) -> None:
        """Proves the link and the PLC program are alive; re-reads everything now and then."""
        if not await self.client.ping():
            raise UpdateFailed("the PLC program stopped")
        if self._attention.is_set() or time.monotonic() - self._synced_at >= const.RESYNC_INTERVAL:
            self._attention.clear()
            self.async_set_updated_data(await self._load())

    # ----- callbacks from the client -----------------------------------------------------

    def _wake_up(self) -> None:
        self._attention.set()

    def _on_change(self, name: str) -> None:
        if not self._ready or not self.last_update_success:
            return
        if name == PLC_RUN:
            self._wake_up()                      # the supervisor pings and decides
        elif self._flush_handle is None:
            self._flush_handle = self.hass.loop.call_later(const.FLUSH_DELAY, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        if self.last_update_success and self.client.connected:
            self.async_set_updated_data(faces_from_values(self.client.values, self.serial))

    # ----- writes ------------------------------------------------------------------------

    async def async_write(self, var_path: str, field: str, value: Any) -> None:
        """Write one field of an object; the PLC's DIFF (or a read-back) updates the entity."""
        try:
            await self.client.set(f"{var_path}.{field}", value)
        except PlccomsError as err:
            raise HomeAssistantError(f"Foxtrot did not take {var_path}.{field}={value!r}: {err}") from err
