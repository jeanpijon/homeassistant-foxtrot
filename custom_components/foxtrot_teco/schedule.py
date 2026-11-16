"""The weekly program of a room thermostat (iControlLib ``T_TIME_PROG_WEEK``) and its JSON file.

The PLC keeps one file per program, ``WWW/iFOX/WTP_<CRC-16 of the name>.JSN``, and runs it by itself.
For a day it counts the leading marks of ``times`` (up to ``numTimes``) that are not later than now,
``k``, and uses the mode ``values[k]`` (0 limit, 1 eco, 2 night, 3 day; ``values[0]`` is only used
before the first mark). A mark that is earlier than a mark before it cannot be reached.

``TimeProgram`` reads the file in either form the PLC accepts (the one it writes itself, or the
compact one of the app), writes exactly the form the PLC writes, and converts the week to and from
blocks of a Home Assistant schedule helper, whose blocks carry ``data: {mode: <preset name>}``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from .thermostat import MODE_PRESETS, PRESET_MODES

DAY = 24 * 3600
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
KEYWORD = "timeProgWeekData"
SLOTS = 7            # marks per day in the file
VALUES = 8           # modes per day in the file

Week = list[list[tuple[int, str]]]      # per weekday: [(start in seconds, preset name)], first start is 0


class ScheduleError(ValueError):
    """The program or the helper cannot be turned into the other."""


def _seconds(text: str) -> int:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{1,2})(?:\.\d+)?)?", text.strip())
    if not match:
        raise ScheduleError(f"not a time of day: {text!r}")
    hours, minutes, seconds = int(match[1]), int(match[2]), int(match[3] or 0)
    return hours * 3600 + minutes * 60 + seconds


def _clock(seconds: int) -> str:
    """The PLC's ``%Thh:mm:s`` (seconds without a leading zero)."""
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60}"


def _hms(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _end_of_block(text: str) -> int:
    """The end of a helper block; Home Assistant may say 24:00:00 or just before midnight."""
    seconds = _seconds(text)
    return DAY if seconds >= DAY - 1 else seconds


def _real(value: float) -> str:
    return f"{value:.1f}"


@dataclass(frozen=True)
class TimeProgram:
    name: str
    typ: int
    num_times: int
    units: int
    heat_temp: tuple[float, ...]                 # per mode 0..3
    cool_temp: tuple[float, ...]
    times: tuple[tuple[int, ...], ...]           # [7 days][7 marks], seconds from midnight
    values: tuple[tuple[int, ...], ...]          # [7 days][8 modes]
    holiday: tuple[str, str] = ("1970-01-01-00:00:0", "1970-01-01-00:00:0")

    # ----- the file -----------------------------------------------------------------------

    @classmethod
    def loads(cls, text: str) -> "TimeProgram":
        try:
            document = json.loads(text)
            data = document[KEYWORD]
            times = tuple(tuple(_seconds(t) for t in row) + (DAY,) * (SLOTS - len(row)) for row in data["times"])
            values = tuple(tuple(int(v) for v in row) + (0,) * (VALUES - len(row)) for row in data["values"])
            program = cls(
                name=str(document.get("Name", "")),
                typ=int(data["typ"]), num_times=int(data["numTimes"]), units=int(data.get("units", 0)),
                heat_temp=tuple(float(t) for t in data["heatTemp"]),
                cool_temp=tuple(float(t) for t in data["coolTemp"]),
                times=times, values=values,
                holiday=tuple(data.get("holiday", cls.holiday))[:2],     # type: ignore[arg-type]
            )
        except (ValueError, KeyError, TypeError) as err:
            if isinstance(err, ScheduleError):
                raise
            raise ScheduleError(f"not a time program file: {err!r}") from err
        if (len(program.times), len(program.values)) != (7, 7) or len(program.heat_temp) != 4 \
                or len(program.cool_temp) != 4 or not 2 <= program.num_times <= SLOTS:
            raise ScheduleError("the time program has an unexpected shape")
        return program

    def dumps(self) -> str:
        """The text exactly as the PLC itself writes the file (one line)."""
        rows = ",".join("[" + ",".join(f'"{_clock(t)}"' for t in row) + "]" for row in self.times)
        modes = ",".join("[" + ",".join(str(v) for v in row) + "]" for row in self.values)
        return (
            f'{{"Name": "{self.name}","{KEYWORD}": {{"typ":{self.typ},"numTimes":{self.num_times},'
            f'"units":{self.units},"heatTemp":[{",".join(_real(t) for t in self.heat_temp)}],'
            f'"coolTemp":[{",".join(_real(t) for t in self.cool_temp)}],"times":[{rows}],'
            f'"values":[{modes}],"holiday":["{self.holiday[0]}","{self.holiday[1]}"]}}}}'
        )

    # ----- what the PLC does with it ------------------------------------------------------

    def week(self) -> Week:
        """The mode over each day as the PLC evaluates it: [(start, preset)] with equal neighbours merged."""
        week: Week = []
        for times, values in zip(self.times, self.values):
            segments: list[tuple[int, int]] = [(0, values[0])]           # before the first mark
            reached = 0
            for k in range(1, self.num_times + 1):
                reached = max(reached, times[k - 1])                      # all marks so far must have passed
                segments.append((reached, values[k]))
            week.append(_merge([(start, MODE_PRESETS[min(3, max(0, mode))]) for start, mode in segments]))
        return week

    def with_week(self, week: Week) -> "TimeProgram":
        """The same program (temperatures, holiday, ...) with another weekly plan."""
        times, values = [], []
        for day, segments in zip(WEEKDAYS, week):
            if len(segments) > self.num_times:
                raise ScheduleError(f"{day} needs {len(segments)} intervals, the thermostat holds at most {self.num_times}")
            starts = [start for start, _ in segments]
            modes = [PRESET_MODES[mode] for _, mode in segments]
            times.append(tuple([0] + starts[1:] + [DAY] * (SLOTS - len(segments))))
            values.append(tuple([modes[0]] + modes + [0] * (VALUES - 1 - len(modes))))
        return replace(self, times=tuple(times), values=tuple(values))


def _merge(segments: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Drop empty intervals and join neighbours with the same mode."""
    merged: list[tuple[int, str]] = []
    for index, (start, mode) in enumerate(segments):
        end = segments[index + 1][0] if index + 1 < len(segments) else DAY
        if end <= start:
            continue
        if merged and merged[-1][1] == mode:
            continue
        merged.append((start, mode))
    return merged or [(0, segments[0][1])]


# ----- Home Assistant schedule helper <-> week ------------------------------------------------


def week_from_helper(schedule: dict[str, list[dict[str, Any]]], base_mode: str) -> Week:
    """Blocks of a schedule helper (``schedule.get_schedule``) as a week; gaps get ``base_mode``."""
    if base_mode not in PRESET_MODES:
        raise ScheduleError(f"unknown base mode {base_mode!r}")
    week: Week = []
    for day in WEEKDAYS:
        blocks = []
        for block in schedule.get(day) or []:
            mode = (block.get("data") or {}).get("mode")
            start, end = _seconds(block["from"]), _end_of_block(block["to"])
            if mode not in PRESET_MODES:
                raise ScheduleError(f"{day} {_hms(start)}-{_hms(end)}: the block needs data mode: one of "
                                    f"{', '.join(PRESET_MODES)} (it has {mode!r})")
            blocks.append((start, end, mode))
        blocks.sort()
        segments: list[tuple[int, str]] = []
        cursor = 0
        for start, end, mode in blocks:
            if start < cursor:
                raise ScheduleError(f"{day}: blocks overlap at {_hms(start)}")
            if start > cursor:
                segments.append((cursor, base_mode))
            segments.append((start, mode))
            cursor = end
        if cursor < DAY:
            segments.append((cursor, base_mode))
        week.append(_merge(segments))
    return week


def helper_from_week(week: Week, base_mode: str) -> dict[str, list[dict[str, Any]]]:
    """The blocks of a schedule helper for a week: every interval that is not ``base_mode``."""
    schedule: dict[str, list[dict[str, Any]]] = {}
    for day, segments in zip(WEEKDAYS, week):
        blocks = []
        for index, (start, mode) in enumerate(segments):
            end = segments[index + 1][0] if index + 1 < len(segments) else DAY
            if mode != base_mode:
                blocks.append({"from": _hms(start), "to": _hms(end), "data": {"mode": mode}})
        schedule[day] = blocks
    return schedule
