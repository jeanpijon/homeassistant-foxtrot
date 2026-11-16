"""Mirrors the PLC's ha_* objects, pushed by PLCComS, and keeps the link up."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import const
from .const import CONF_SERIAL, DOMAIN, HA_PREFIX
from .plccoms import PLC_RUN, PlccomsClient, PlccomsError
from .schedule import ScheduleError, TimeProgram

_LOGGER = logging.getLogger(__name__)

FIELD_RE = re.compile(rf"^(?P<obj>{re.escape(HA_PREFIX)}[^.]+)\.(?P<field>\w+)$")
TPW_RE = re.compile(rf"^(?P<prefix>.+)\.{re.escape(const.TPW)}(?P<field>\w+)$")


def faces_from_values(values: dict[str, Any], serial: str) -> dict[str, dict[str, Any]]:
    """Group ``ha_x.field`` variables into one face per object, as {var_path: {field: value}}.

    An object without a ``kind`` field cannot be told apart (sensor, switch, climate) and is skipped.
    A climate object whose ``prog`` is the name of a thermostat's time program gets that program's
    variables as ``_tpw`` ({field: value, "_prefix": path of the block}).
    """
    faces: dict[str, dict[str, Any]] = {}
    programs: dict[str, dict[str, Any]] = {}
    for name, value in values.items():
        if match := FIELD_RE.match(name):
            faces.setdefault(match["obj"], {})[match["field"]] = value
        elif match := TPW_RE.match(name):
            programs.setdefault(match["prefix"], {"_prefix": match["prefix"]})[match["field"]] = value
    faces = {path: face for path, face in faces.items() if "kind" in face}
    by_name: dict[Any, list[dict[str, Any]]] = {}
    for program in programs.values():
        by_name.setdefault(program.get("name"), []).append(program)
    for path, face in faces.items():
        face["_serial"] = serial
        face["_var_path"] = path
        # Two blocks with one name cannot be told apart: the climate object stays unlinked.
        if face.get("prog") and len(by_name.get(face["prog"], ())) == 1:
            face["_tpw"] = by_name[face["prog"]][0]
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
        self.command_lock = asyncio.Lock()       # one command sequence to the time programs at a time
        self.programs: dict[str, TimeProgram] = {}   # block path -> its weekly program, as last read
        self._program_reads: dict[str, asyncio.Task] = {}

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
        for task in [self._task, *self._program_reads.values()]:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._task = None
        self._program_reads.clear()
        await self.client.close()

    async def _sync(self) -> dict[str, dict[str, Any]]:
        """(Re)connect, read everything, and have the PLC push changes from now on."""
        try:
            if not self.client.connected:
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

        # The thermostats' time programs, if the PLC publishes them (commands need them enabled, too).
        programs = await client.list_variables(f"*.{const.TPW}*")
        wanted = {**variables, **programs}

        for name, plc_type in wanted.items():
            real = plc_type.upper() in ("REAL", "LREAL")
            await client.enable(name, const.REAL_DELTA if real else None)
        await client.enable(PLC_RUN)
        answered = set(await client.snapshot(f"{HA_PREFIX}*"))
        if programs:
            answered |= set(await client.snapshot(f"*.{const.TPW}*"))
        if missing := set(wanted) - answered:
            raise UpdateFailed(f"PLCComS returned {len(answered)} of {len(wanted)} variables "
                               f"(not enabled: {', '.join(sorted(missing)[:3])}...)")

        faces = faces_from_values({n: client.values[n] for n in wanted if n in client.values}, self.serial)
        for path, face in faces.items():
            if face.get("prog") and "_tpw" not in face:
                _LOGGER.warning("%s names the time program %r, which the PLC does not publish exactly once; "
                                "presets and schedule are not available for it", path, face["prog"])
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
        elif name.endswith(f".{const.TPW}crc"):
            self._program_changed(name.rsplit(".", 1)[0])
            self._schedule_flush()
        else:
            self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_handle is None:
            self._flush_handle = self.hass.loop.call_later(const.FLUSH_DELAY, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        if self.last_update_success and self.client.connected:
            self.async_set_updated_data(faces_from_values(self.client.values, self.serial))

    # ----- writes ------------------------------------------------------------------------

    def tpw(self, prefix: str, field: str) -> Any:
        """Current value of a time-program variable (None while unknown)."""
        return self.client.values.get(f"{prefix}.{const.TPW}{field}")

    # ----- weekly programs -----------------------------------------------------------------

    def _program_changed(self, prefix: str) -> None:
        """The PLC reports another program CRC for this block: read the file again (once at a time)."""
        if prefix in self._program_reads or prefix not in self.programs:
            return
        task = self.config_entry.async_create_background_task(
            self.hass, self._reread_program(prefix), f"{DOMAIN} program {prefix}")
        self._program_reads[prefix] = task
        task.add_done_callback(lambda _: self._program_reads.pop(prefix, None))

    async def _reread_program(self, prefix: str) -> None:
        try:
            await self.async_read_program(prefix)
        except (PlccomsError, ScheduleError) as err:
            _LOGGER.debug("Cannot read the new program of %s: %s", prefix, err)
        else:
            self.async_update_listeners()

    async def async_read_program(self, prefix: str) -> TimeProgram:
        """The weekly program the PLC runs for this thermostat block (read from its file)."""
        path = self.tpw(prefix, "file")
        if not path:
            raise HomeAssistantError(f"the PLC does not name a program file for {prefix}")
        try:
            program = TimeProgram.loads(await self.client.get_file(path))
        except (PlccomsError, ScheduleError) as err:
            raise HomeAssistantError(f"cannot read {path}: {err}") from err
        self.programs[prefix] = program
        return program

    async def async_load_programs(self) -> None:
        """Read the program of every thermostat block that belongs to a climate object."""
        for face in self.data.values():
            if tpw := face.get("_tpw"):
                try:
                    await self.async_read_program(tpw["_prefix"])
                except HomeAssistantError as err:
                    _LOGGER.warning("%s", err)

    async def async_write_program(self, prefix: str, program: TimeProgram) -> None:
        """Have the PLC run ``program`` for this block, as the iFoxtrot app does: leave it as a .APP
        file next to the program's file, tell the block to load it, and check what the PLC now holds."""
        async with self.command_lock:
            path = self.tpw(prefix, "file")
            if not path:
                raise HomeAssistantError(f"the PLC does not name a program file for {prefix}")
            crc = self.tpw(prefix, "crc")
            try:
                await self.client.write_file(path.rsplit(".", 1)[0] + ".APP", program.dumps())
            except PlccomsError as err:
                raise HomeAssistantError(f"Foxtrot did not take the program for {path}: {err}") from err
            await self.async_pulse(prefix, "update", True, lambda: self.tpw(prefix, "crc") != crc,
                                   "load the new weekly program", timeout=const.PROGRAM_TIMEOUT)
            now = await self.async_read_program(prefix)
            if (now.week(), now.heat_temp, now.cool_temp, now.holiday) != \
                    (program.week(), program.heat_temp, program.cool_temp, program.holiday):
                raise HomeAssistantError(f"Foxtrot loaded the program of {path}, but it differs from what was sent")
        self.async_update_listeners()

    async def async_pulse(self, prefix: str, field: str, value: Any, effect: Callable[[], bool], what: str,
                          timeout: float | None = None) -> None:
        """Command a room's time program and wait until the PLC shows ``effect``.

        The program consumes a command within a scan and resets it, so a second command must not
        go out before the first one took effect (it would merge with it). Nothing is sent when the
        effect is already there.
        """
        if effect():
            return
        try:
            await self.client.pulse(f"{prefix}.{const.TPW}{field}", value)
        except PlccomsError as err:
            raise HomeAssistantError(f"Foxtrot did not take the command to {what}: {err}") from err
        wait = timeout or const.TPW_TIMEOUT
        try:
            async with asyncio.timeout(wait):
                while not effect():
                    await asyncio.sleep(const.TPW_POLL)
        except TimeoutError:
            raise HomeAssistantError(f"Foxtrot did not {what}: no change reported within {wait:g} s") from None

    async def async_write(self, var_path: str, field: str, value: Any) -> None:
        """Write one field of an object; the PLC's DIFF (or a read-back) updates the entity."""
        try:
            await self.client.set(f"{var_path}.{field}", value)
        except PlccomsError as err:
            raise HomeAssistantError(f"Foxtrot did not take {var_path}.{field}={value!r}: {err}") from err
