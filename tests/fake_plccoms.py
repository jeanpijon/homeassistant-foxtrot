"""A stand-in for the PLCComS server of a Foxtrot, speaking the protocol as observed on a CP-2000.

Variables start disabled for each connection; ``EN:`` enables them (no answer); a wildcard ``GET:``
returns the enabled ones; changes of enabled ones are pushed as ``DIFF:``; ``SET:`` is answered with
``DIFF:``. Names are upper-cased on the wire. Tests drive it: ``push`` changes a value, ``drop``
cuts the connections, ``replace_program`` swaps the variables and warns the clients. ``TpwSim``
plays the time-program block of a thermostat (the PLC program reacting to command variables).
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
        self.upload: dict[str, list[str]] = {}
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
        self.sims: list[TpwSim] = []             # PLC program parts reacting to SET
        self.files: dict[str, str] = {}          # the PLC's file system, upper-cased "//path" -> text
        self.ignore_close = False                # WRITEFILE never gets its OK (the closing block is "lost")
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
        before = dict(self.variables)
        self.variables[name] = (self.variables[name][0], value)
        self.announce(before)

    def announce(self, before: dict, answer: _Conn | None = None, answered: str | None = None) -> None:
        """Send ``DIFF:`` for every variable that differs from ``before`` to the connections that enabled it
        (and the answer to a ``SET`` to the connection that sent it)."""
        for name, (plc_type, value) in list(self.variables.items()):
            old = before.get(name, (plc_type, value))[1]
            if old == value:
                continue
            for conn in self.conns:
                is_answer = conn is answer and name == answered
                if is_answer and self.swallow_diff_on_set:
                    continue
                if not is_answer:
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
        conn.writer.write((line + "\r\n").encode("cp1250"))

    def _match(self, pattern: str) -> list[str]:
        return [n for n in self.variables if fnmatch.fnmatchcase(n, pattern.upper())]

    async def _serve(self, reader, writer):
        conn = _Conn(reader, writer)
        self.conns.append(conn)
        self.connections_made += 1
        try:
            while line := await reader.readline():
                text = line.decode("cp1250").strip()
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
            if name not in conn.enabled and name not in SYSTEM:
                self._send(conn, f"ERROR:34 Disabled register name: '{name}'")
                return
            if self.ignore_set:
                return
            plc_type, _old = self.variables[name]
            before = dict(self.variables)
            self.variables[name] = (plc_type, parse(value, plc_type))
            for sim in self.sims:                # the PLC scan may consume the command at once
                sim.on_set(name)
            self.announce(before, answer=conn, answered=name)
        elif command == "GETFILE":
            if arg.upper() in self.files:
                text = self.files[arg.upper()]
                self._send(conn, f"GETFILE:{arg}[{len(text.encode('cp1250'))}]={text}")
                self._send(conn, f"GETFILE:{arg}[0]=")
            else:
                self._send(conn, f"ERROR:41 Unable to get file: '{arg}'")
        elif command == "WRITEFILE":
            head, _, data = arg.partition("=")
            name, _, size = head.rpartition("[")
            size = int(size.rstrip("]") or 0)
            if size:
                conn.upload.setdefault(name.upper(), []).append(data)
            elif not self.ignore_close:
                self.files[name.upper()] = "".join(conn.upload.pop(name.upper(), []))
                self._send(conn, f"WRITEFILE:{name} OK")
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


TPW_FIELDS = {
    "COOL": "BOOL", "COOLTEMP": "REAL", "CRC": "UINT", "DECDELTA": "BOOL", "DELTA": "REAL", "ENABLE": "BOOL",
    "FILE": "STRING[24]", "HEAT": "BOOL", "HEATTEMP": "REAL", "HOLIDAY": "BOOL", "INCDELTA": "BOOL",
    "MANUAL": "BOOL", "MANUALSET": "SINT", "MODE": "SINT", "MODESET": "SINT", "NAME": "STRING[24]",
    "NEXTMODE": "BOOL", "NEXTPROG": "BOOL", "ROOMTEMP": "REAL", "TYPE": "USINT", "UPDATE": "BOOL",
}


class TpwSim:
    """The time-program block of one thermostat (iControlLib fb_TimeProgWeek_Control).

    Commands are variables the program consumes and resets: ``manualset``, ``modeset`` (a mode set
    in auto is overwritten by the schedule), ``incdelta``/``decdelta`` (0.5 K, +-5 K, auto -> manual).
    ``scan_delay`` 0 reacts inside the ``SET`` (the reset wins against the server's poll, no ``DIFF``
    shows the command); above 0 the scan comes later, as a separate push.
    """

    def __init__(self, plc: FakePlccoms, prefix: str, name: str, heat_temps=(5.0, 19.0, 21.0, 23.0),
                 schedule_mode: int = 2, scan_delay: float = 0.0, program: str | None = None):
        self.plc, self.prefix = plc, prefix.upper()
        self.rejected = 0                        # uploads the program refused
        self.reject = False                      # ignore every upload (a block that does not react)
        self.heat_temps, self.schedule_mode, self.scan_delay = list(heat_temps), schedule_mode, scan_delay
        self.manual, self.mode, self.delta = False, schedule_mode, 0.0
        self.scans = 0
        self.initial = {
            "NAME": name, "FILE": f"WWW/iFOX/WTP_{sum(name.encode()) % 0x10000:04X}.JSN", "CRC": 1234,
            "ENABLE": True, "TYPE": 3, "MANUALSET": -1, "MODESET": -1, "COOLTEMP": 28.0,
            "ROOMTEMP": 21.0, "HEAT": False, "COOL": False, "HOLIDAY": False,
            "MANUAL": False, "MODE": schedule_mode, "DELTA": 0.0, "HEATTEMP": heat_temps[schedule_mode],
        }
        plc.sims.append(self)
        if program is not None:
            plc.files[self.file_key()] = program
            self.initial["CRC"] = self.crc_of(program)

    def file_key(self, ext: str = "JSN") -> str:
        return "//" + self.initial["FILE"].upper().rsplit(".", 1)[0] + "." + ext

    @staticmethod
    def crc_of(text: str) -> int:
        import zlib
        return zlib.crc32(text.encode("cp1250")) & 0xFFFF

    def variables(self) -> dict[str, tuple[str, Any]]:
        default = {"BOOL": False, "REAL": 0.0, "UINT": 0, "SINT": 0, "USINT": 0, "STRING[24]": ""}
        return {self._full(f): (t, self.initial.get(f, default[t])) for f, t in TPW_FIELDS.items()}

    def _full(self, field: str) -> str:
        return f"{self.prefix}.GTSAP1_TPW_{field}"

    def get(self, field: str):
        return self.plc.variables[self._full(field)][1]

    def put(self, field: str, value) -> None:
        name = self._full(field)
        self.plc.variables[name] = (self.plc.variables[name][0], value)

    def on_set(self, name: str) -> None:
        if not name.startswith(self.prefix + ".GTSAP1_TPW_"):
            return
        if self.scan_delay:
            asyncio.get_running_loop().call_later(self.scan_delay, self.scan_and_announce)
        else:
            self.scan()

    def scan_and_announce(self) -> None:
        before = dict(self.plc.variables)
        self.scan()
        self.plc.announce(before)

    def next_interval(self, mode: int) -> None:
        """The weekly program moves on: a manual setting ends, the new interval's mode applies."""
        self.schedule_mode, self.manual = mode, False
        self.scan_and_announce()

    def scan(self) -> None:
        self.scans += 1
        get, put = self.get, self.put
        if get("MANUALSET") != -1:
            self.manual = get("MANUALSET") == 1
            put("MANUALSET", -1)
        if get("MODESET") != -1:
            self.mode = max(0, min(3, get("MODESET")))
            put("MODESET", -1)
        if get("UPDATE"):
            put("UPDATE", False)
            self.load_upload()
        inc, dec = get("INCDELTA"), get("DECDELTA")
        if not self.manual and (inc or dec):
            self.manual = True
        if self.manual:
            if inc:
                self.delta = min(5.0, self.delta + 0.5)
                put("INCDELTA", False)
            if dec:
                self.delta = max(-5.0, self.delta - 0.5)
                put("DECDELTA", False)
        else:
            self.mode, self.delta = self.schedule_mode, 0.0
        put("MANUAL", self.manual)
        put("MODE", self.mode)
        put("DELTA", self.delta)
        put("HEATTEMP", self.heat_temps[self.mode] + self.delta)

    def load_upload(self) -> None:
        """The program takes the file the app left as .APP (and removes it), or ignores a bad one."""
        import json
        text = self.plc.files.pop(self.file_key("APP"), None)
        if text is None or self.reject:
            return
        try:
            data = json.loads(text)["timeProgWeekData"]
            assert len(data["times"]) == 7 and len(data["values"]) == 7
        except Exception:
            self.rejected += 1
            return
        self.plc.files[self.file_key()] = text
        self.put("CRC", self.crc_of(text))
        self.loaded = data
