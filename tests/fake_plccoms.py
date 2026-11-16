"""A stand-in for the PLCComS server of a Foxtrot, speaking the protocol as observed on a CP-2000.

Variables start disabled for each connection; ``EN:`` enables them (no answer); a wildcard ``GET:``
returns the enabled ones; changes of enabled ones are pushed as ``DIFF:``; ``SET:`` is answered with
``DIFF:``. Names are upper-cased on the wire. Tests drive it: ``push`` changes a value, ``drop``
cuts the connections, ``replace_program`` swaps the variables and warns the clients.
"""
from __future__ import annotations

import asyncio
import fnmatch
from typing import Any

SYSTEM = {"__PLC_RUN": ("BOOL", True), "__PF_CRC": ("DWORD", 1)}


def wire(value: Any, plc_type: str) -> str:
    if plc_type.startswith("STRING"):
        return f'"{value}"'
    if plc_type == "BOOL":
        return "1" if value else "0"
    if plc_type in ("REAL", "LREAL"):
        return f"{float(value):.6f}"
    return str(int(value))


def parse(text: str, plc_type: str):
    text = text.strip()
    if plc_type.startswith("STRING"):
        return text.strip('"')
    if plc_type == "BOOL":
        return text in ("1", "true")
    if plc_type in ("REAL", "LREAL"):
        return float(text)
    return int(text)


class _Conn:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.enabled: set[str] = set()
        self.delta: dict[str, float] = {}


class FakePlccoms:
    def __init__(self, variables: dict[str, tuple[str, Any]] | None = None):
        # name (any case) -> (PLC type, value)
        self.variables: dict[str, tuple[str, Any]] = {}
        self.set_variables(variables or {})
        self.conns: list[_Conn] = []
        self.commands: list[str] = []            # every command line received, in order
        self.mute = False                        # stop answering anything (a hung PLC)
        self.swallow_diff_on_set = False         # SET changes the value but sends no DIFF
        self.ignore_set = False                  # SET changes nothing and sends nothing (a PLC that refuses)
        self.inject: list[tuple[str, Any]] = []  # (name, value) changes pushed in the middle of the next wildcard GET answer
        self.server: asyncio.Server | None = None
        self.port = 0
        self.connections_made = 0

    def set_variables(self, variables: dict[str, tuple[str, Any]]) -> None:
        self.variables = {name.upper(): v for name, v in {**SYSTEM, **variables}.items()}

    async def start(self, port: int = 0) -> int:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        await self.drop()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def drop(self) -> None:
        """Cut every connection, as a power cut or a cable would."""
        for conn in list(self.conns):
            conn.writer.close()
        self.conns.clear()
        await asyncio.sleep(0)

    # ----- driven by the tests -----------------------------------------------------------

    def push(self, name: str, value: Any) -> None:
        """The PLC program changed a variable: tell the connections that enabled it."""
        name = name.upper()
        plc_type, old = self.variables[name]
        self.variables[name] = (plc_type, value)
        for conn in self.conns:
            if name not in conn.enabled and name not in SYSTEM:
                continue
            if plc_type in ("REAL", "LREAL") and abs(float(value) - float(old)) <= conn.delta.get(name, 0.0):
                continue
            self._send(conn, f"DIFF:{name},{wire(value, plc_type)}")

    async def replace_program(self, variables: dict[str, tuple[str, Any]]) -> None:
        """A new PLC program was loaded: every connection loses what it enabled and is warned."""
        self.set_variables(variables)
        for conn in self.conns:
            conn.enabled.clear()
            self._send(conn, "WARNING:250 Changed public file: 'iFoxtrot.pub'")
        await asyncio.sleep(0)

    # ----- the server --------------------------------------------------------------------

    def _send(self, conn: _Conn, line: str) -> None:
        conn.writer.write((line + "\r\n").encode())

    def _match(self, pattern: str) -> list[str]:
        return [n for n in self.variables if fnmatch.fnmatchcase(n, pattern.upper())]

    async def _serve(self, reader, writer):
        conn = _Conn(reader, writer)
        self.conns.append(conn)
        self.connections_made += 1
        try:
            while line := await reader.readline():
                text = line.decode().strip()
                self.commands.append(text)
                if not self.mute:
                    self._handle(conn, text)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if conn in self.conns:
                self.conns.remove(conn)
            writer.close()

    def _handle(self, conn: _Conn, text: str) -> None:
        command, _, arg = text.partition(":")
        command = command.upper()
        if command == "LIST":
            for name in self._match(arg or "*"):
                star = "" if name in SYSTEM or name in conn.enabled else "*"
                self._send(conn, f"LIST:{name},{self.variables[name][0]}{star}")
            self._send(conn, "LIST:")
        elif command == "GET":
            if "*" in arg:
                for name in self._match(arg):
                    if name in SYSTEM or name in conn.enabled:
                        plc_type, value = self.variables[name]
                        self._send(conn, f"GET:{name},{wire(value, plc_type)}")
                        while self.inject:
                            self.push(*self.inject.pop(0))
                self._send(conn, "GET:")
            elif arg.upper() in self.variables:
                plc_type, value = self.variables[arg.upper()]
                self._send(conn, f"GET:{arg.upper()},{wire(value, plc_type)}")
            else:
                self._send(conn, f"ERROR:33 Unknown register name: '{arg.upper()}'")
        elif command in ("EN", "DI"):
            pattern, _, delta = arg.partition(" ")
            for name in self._match(pattern or "*"):
                if command == "EN":
                    conn.enabled.add(name)
                    conn.delta[name] = float(delta) if delta else 0.0
                else:
                    conn.enabled.discard(name)
        elif command == "SET":
            name, _, value = arg.partition(",")
            name = name.upper()
            if name not in self.variables:
                self._send(conn, f"ERROR:33 Unknown register name: '{name}'")
                return
            if self.ignore_set:
                return
            plc_type, old = self.variables[name]
            new = parse(value, plc_type)
            self.variables[name] = (plc_type, new)
            if new != old and not self.swallow_diff_on_set:
                self._send(conn, f"DIFF:{name},{wire(new, plc_type)}")
        elif command == "GETINFO":
            if arg.upper() == "VERSION_PLC":
                self._send(conn, "GETINFO:VERSION_PLC,CP2000I   M 2.7 2.0")
            else:
                self._send(conn, "GETINFO:")
        else:
            self._send(conn, f"ERROR:32 Unknown command name: '{command}'")


def variables_from_contract(contract: dict, samples: dict | None = None) -> dict[str, tuple[str, Any]]:
    """Flatten tests/fixtures/ha_contract.json samples into the variables a PLC would list."""
    samples = samples if samples is not None else contract["samples"]
    out: dict[str, tuple[str, Any]] = {}
    for obj, face in samples.items():
        kind = face["kind"]
        type_name = {1: "T_HA_Climate", 2: "T_HA_Sensor", 3: "T_HA_Binary"}[kind]
        for field, plc_type in contract["types"][type_name]["fields"].items():
            if field in face:
                out[f"{obj}.{field}"] = ("STRING[64]" if plc_type == "STRING" else plc_type, face[field])
    return out
