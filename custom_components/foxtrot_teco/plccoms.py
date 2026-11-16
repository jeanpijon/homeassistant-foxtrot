"""Client for the PLCComS text protocol that a TECO Foxtrot PLC serves on port 5010.

One TCP connection, one command line per request (``GET:name``), lines end with CRLF. Names are
case-insensitive and always come back upper-cased; this module lower-cases them. What matters
about the protocol, as observed on a CP-2000 (PLCComS 5.4):

* ``LIST:`` / ``GET:`` with a ``*`` wildcard answer with one line per variable and a bare
  ``LIST:`` / ``GET:`` line at the end. A ``GET:`` of an exact name answers with one line.
* Variables start *disabled* for a connection (the type in ``LIST`` ends with ``*``). A wildcard
  ``GET`` returns only enabled variables, and only enabled ones are pushed. ``EN:name [delta]``
  enables them; it has no answer. A bare ``EN:`` enables everything.
* Once enabled, a change is pushed as ``DIFF:name,value`` (REAL only beyond ``delta``).
* ``SET:name,value`` is answered with ``DIFF:name,value`` (probably not when nothing changed).
* ``ERROR:33 Unknown register name: 'X'``; ``WARNING:250`` means the PLC program (the public
  file) was replaced.
* Values: BOOL ``1``/``0``, REAL ``23.900000``, STRING ``"text"``.

The PLC answers about four commands a second, and two wildcard commands sent back to back may
lose a terminator, so commands are strictly one at a time.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 5010
CONNECT_TIMEOUT = 10
COMMAND_TIMEOUT = 5
SET_CONFIRM_TIMEOUT = 1.5

# Always present, never disabled; not listed by the user's public file.
SYSTEM_TYPES = {"__plc_run": "BOOL", "__pf_crc": "DWORD"}
PLC_RUN = "__plc_run"

_REAL_TYPES = {"REAL", "LREAL"}
_INT_TYPES = {"SINT", "USINT", "INT", "UINT", "DINT", "UDINT", "LINT", "ULINT", "BYTE", "WORD", "DWORD", "LWORD"}


class PlccomsError(Exception):
    """Anything that went wrong talking to PLCComS."""


class PlccomsConnectionError(PlccomsError):
    """No connection, or it was lost or went silent."""


class PlccomsCommandError(PlccomsError):
    """The server answered ``ERROR:<code> <text>``."""

    def __init__(self, code: int, text: str):
        super().__init__(f"PLCComS error {code}: {text}")
        self.code = code


def convert(raw: str, plc_type: str | None):
    """Turn the text of a value into a Python value, following the PLC type of the variable."""
    raw = raw.strip()
    kind = (plc_type or "").upper().rstrip("*")
    try:
        if kind.startswith("STRING") or (not kind and raw.startswith('"')):
            return raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw
        if kind == "BOOL":
            return raw.lower() in ("1", "true")
        if kind in _REAL_TYPES:
            return float(raw)
        if kind in _INT_TYPES:
            return int(raw)
        if not kind:
            return int(raw) if raw.lstrip("-").isdigit() else float(raw)
    except ValueError:
        pass
    return raw


def format_value(value: Any, plc_type: str | None) -> str:
    """The text for ``SET:``."""
    kind = (plc_type or "").upper().rstrip("*")
    if kind.startswith("STRING") or isinstance(value, str):
        return '"' + str(value).replace('"', "'") + '"'
    if kind == "BOOL" or isinstance(value, bool):
        return "1" if value else "0"
    if kind in _INT_TYPES:
        return str(int(value))
    return repr(float(value))


def _split(rest: str) -> tuple[str, str]:
    """``NAME,value`` -> (lower-case name, raw value); a string value may itself contain commas."""
    name, _, value = rest.partition(",")
    return name.strip().lower(), value


@dataclass
class _Pending:
    command: str                      # LIST | GET | SET | GETINFO
    target: str                       # lower-case name, or the pattern
    future: asyncio.Future
    wildcard: bool = False
    names: list[str] = field(default_factory=list)

    def done(self, result=None) -> None:
        if not self.future.done():
            self.future.set_result(result)

    def fail(self, err: Exception) -> None:
        if not self.future.done():
            self.future.set_exception(err)


class PlccomsClient:
    """A single PLCComS connection; reconnecting is the caller's business."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        on_change: Callable[[str], None] | None = None,
        on_reload: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ):
        self.host = host
        self.port = port
        self.on_change = on_change
        self.on_reload = on_reload
        self.on_disconnect = on_disconnect
        self.types: dict[str, str] = dict(SYSTEM_TYPES)   # name -> PLC type, filled by list_variables()
        self.values: dict[str, Any] = {}                  # name -> last value, kept current by DIFF and GET
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._pending: _Pending | None = None

    @property
    def connected(self) -> bool:
        return self._task is not None and not self._task.done()

    async def connect(self) -> None:
        await self.close()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT
            )
        except (OSError, TimeoutError) as err:
            raise PlccomsConnectionError(f"cannot connect to {self.host}:{self.port}: {err}") from err
        sock = self._writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.values.clear()
        self._task = asyncio.create_task(self._read_loop(self._reader, self._writer), name="foxtrot plccoms reader")

    async def close(self) -> None:
        task, writer = self._task, self._writer
        self._task = self._reader = self._writer = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        self._fail_pending(PlccomsConnectionError("connection closed"))

    # ----- reading -----------------------------------------------------------------------

    async def _read_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        err: Exception = PlccomsConnectionError("connection closed by the PLC")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    self._dispatch(line.decode("cp1250", errors="replace").strip())
                except Exception:  # a bad line must not kill the connection
                    _LOGGER.exception("Cannot handle PLCComS line %r", line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err = PlccomsConnectionError(f"connection lost: {exc}")
        # Ended by itself: tell whoever waits, then the owner.
        if self._task is asyncio.current_task():
            self._task = None
            self._fail_pending(err)
            with contextlib.suppress(Exception):
                writer.close()
            if self.on_disconnect:
                self.on_disconnect()

    def _fail_pending(self, err: Exception) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            pending.fail(err)

    def _store(self, name: str, raw: str) -> bool:
        value = convert(raw, self.types.get(name))
        changed = self.values.get(name, _MISSING) != value
        self.values[name] = value
        return changed

    def _dispatch(self, line: str) -> None:
        command, _, rest = line.partition(":")
        command = command.strip().upper()
        pending = self._pending
        if command == "DIFF":
            name, raw = _split(rest)
            changed = self._store(name, raw)
            if pending and pending.command == "SET" and pending.target == name:
                pending.done()
            if changed and self.on_change:
                self.on_change(name)
        elif command == "GET":
            if not rest.strip():                          # end of a wildcard answer
                if pending and pending.command == "GET" and pending.wildcard:
                    pending.done(pending.names)
                return
            name, raw = _split(rest)
            if self._store(name, raw) and self.on_change:
                self.on_change(name)
            if pending and pending.command == "GET":
                pending.names.append(name)
                if not pending.wildcard and pending.target == name:
                    pending.done(pending.names)
        elif command == "LIST":
            if not rest.strip():
                if pending and pending.command == "LIST":
                    pending.done(pending.names)
                return
            name, plc_type = _split(rest)
            self.types[name] = plc_type.strip().rstrip("*")
            if pending and pending.command == "LIST":
                pending.names.append(name)
        elif command == "GETINFO":
            name, value = _split(rest)
            if pending and pending.command == "GETINFO" and pending.target == name:
                pending.done(value.strip())
        elif command == "ERROR":
            code, _, text = rest.strip().partition(" ")
            err = PlccomsCommandError(int(code) if code.isdigit() else 1024, text)
            if pending:
                self._pending = None
                pending.fail(err)
            else:
                _LOGGER.warning("PLCComS reported: %s", err)
        elif command == "WARNING":
            code = rest.strip().split(" ", 1)[0]
            if code == "250":
                _LOGGER.info("The PLC program changed (public file reloaded)")
                if self.on_reload:
                    self.on_reload()
            else:
                _LOGGER.warning("PLCComS warning: %s", rest.strip())
        else:
            _LOGGER.debug("Unexpected PLCComS line: %r", line)

    # ----- commands ----------------------------------------------------------------------

    def _send(self, text: str) -> None:
        if self._writer is None or not self.connected:
            raise PlccomsConnectionError("not connected")
        self._writer.write(text.encode("cp1250", errors="replace"))

    async def _command(self, line: str, command: str, target: str, *, wildcard: bool = False,
                       timeout: float | None = None, drop_on_timeout: bool = True):
        """Send one command and wait for its answer.

        A silent PLC leaves the conversation in an unknown state, so by default the connection is
        dropped; a caller that expects silence (SET without a change) passes ``drop_on_timeout=False``
        and gets the plain ``TimeoutError``.
        """
        async with self._lock:
            if not self.connected:
                raise PlccomsConnectionError("not connected")
            pending = _Pending(command, target, asyncio.get_running_loop().create_future(), wildcard)
            self._pending = pending
            try:
                self._send(line + "\n")
                async with asyncio.timeout(timeout or COMMAND_TIMEOUT):
                    return await pending.future
            except TimeoutError:
                if not drop_on_timeout:
                    raise
                await self.close()
                raise PlccomsConnectionError(f"no answer to {line!r}") from None
            finally:
                if self._pending is pending:
                    self._pending = None

    async def list_variables(self, pattern: str) -> dict[str, str]:
        """Variables matching ``pattern`` (all of them, enabled or not) as name -> PLC type."""
        names = await self._command(f"LIST:{pattern}", "LIST", pattern.lower(), wildcard=True)
        return {name: self.types[name] for name in names}

    async def enable(self, pattern: str, delta: float | None = None) -> None:
        """Have the PLC push changes of the variables matching ``pattern`` (no answer comes)."""
        self._send(f"EN:{pattern}" + (f" {delta}" if delta is not None else "") + "\n")

    async def snapshot(self, pattern: str) -> list[str]:
        """Read all enabled variables matching ``pattern``; values land in ``self.values``."""
        return await self._command(f"GET:{pattern}", "GET", pattern.lower(), wildcard="*" in pattern)

    async def get(self, name: str):
        name = name.lower()
        await self._command(f"GET:{name}", "GET", name)
        return self.values[name]

    async def get_info(self, key: str) -> str:
        return await self._command(f"GETINFO:{key}", "GETINFO", key.lower())

    async def set(self, name: str, value: Any):
        """Write a variable and return the value the PLC holds afterwards."""
        name = name.lower()
        plc_type = self.types.get(name)
        text = format_value(value, plc_type)
        try:
            await self._command(f"SET:{name},{text}", "SET", name,
                                timeout=SET_CONFIRM_TIMEOUT, drop_on_timeout=False)
        except TimeoutError:
            # No DIFF: the PLC had that value already, or the change was below the delta. Look.
            await self.get(name)
            if not _same(convert(text, plc_type), self.values[name]):
                raise PlccomsError(f"{name} stays {self.values[name]!r} after writing {value!r}") from None
        return self.values[name]

    async def ping(self) -> bool:
        """Is the PLC program running? Also proves the link is alive."""
        return bool(await self.get(PLC_RUN))


_MISSING = object()


def _same(a: Any, b: Any) -> bool:
    """Equal, allowing for the single-precision rounding of a REAL read back from the PLC."""
    return a == b or (isinstance(a, float) and isinstance(b, float) and abs(a - b) < 1e-3)
