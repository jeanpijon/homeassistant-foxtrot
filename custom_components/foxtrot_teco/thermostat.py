"""Room thermostat control through the PLC's time-program block, the way the iFoxtrot app does it.

The PLC decides the setpoint: ``heattemp = program temperature of the current mode + delta``. The
mode comes from the weekly program while the block is in auto, or from a command while it is in
manual; manual ends by itself at the next switch time of the program. Commands are variables the
program consumes and resets (see ``FoxtrotCoordinator.async_pulse``):

* ``manualset`` 0 = auto, 1 = manual; ``modeset`` 0 limit, 1 eco, 2 night, 3 day (a mode set while
  the block is still in auto is overwritten by the program in the same scan: manual comes first);
* ``incdelta`` / ``decdelta`` move the delta by 0.5 K within +-5 K and switch auto to manual.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.climate.const import PRESET_COMFORT, PRESET_ECO, PRESET_SLEEP
from homeassistant.exceptions import HomeAssistantError

PRESET_SCHEDULE = "schedule"     # follow the weekly program (auto)
PRESET_LIMIT = "limit"           # the program's lowest mode

MODE_PRESETS = {3: PRESET_COMFORT, 2: PRESET_SLEEP, 1: PRESET_ECO, 0: PRESET_LIMIT}   # PLC mode -> preset
PRESET_MODES = {preset: mode for mode, preset in MODE_PRESETS.items()}
PRESETS = [PRESET_SCHEDULE, PRESET_COMFORT, PRESET_SLEEP, PRESET_ECO, PRESET_LIMIT]

STEP = 0.5          # K per command
MAX_DELTA = 5.0     # K either way


def preset_of(tpw: dict[str, Any]) -> str | None:
    if not tpw.get("manual"):
        return PRESET_SCHEDULE
    return MODE_PRESETS.get(tpw.get("mode"))


def limits(tpw: dict[str, Any]) -> tuple[float, float] | None:
    """Lowest and highest setpoint the delta can reach with the current mode."""
    try:
        base = float(tpw["heattemp"]) - float(tpw["delta"])
    except (KeyError, TypeError, ValueError):
        return None
    return base - MAX_DELTA, base + MAX_DELTA


async def set_preset(coordinator, prefix: str, preset: str) -> None:
    value = lambda field: coordinator.tpw(prefix, field)           # noqa: E731
    async with coordinator.command_lock:
        if preset == PRESET_SCHEDULE:
            await coordinator.async_pulse(prefix, "manualset", 0, lambda: not value("manual"),
                                          "follow the schedule")
            return
        mode = PRESET_MODES[preset]
        await coordinator.async_pulse(prefix, "manualset", 1, lambda: bool(value("manual")),
                                      "switch to manual")
        await coordinator.async_pulse(prefix, "modeset", mode,
                                      lambda: bool(value("manual")) and value("mode") == mode,
                                      f"select {preset}")


async def set_target(coordinator, prefix: str, target: float) -> None:
    """Move the setpoint to ``target`` in steps of 0.5 K, each confirmed by the PLC before the next."""
    value = lambda field: coordinator.tpw(prefix, field)           # noqa: E731
    async with coordinator.command_lock:
        current, delta = value("heattemp"), value("delta")
        if current is None or delta is None:
            raise HomeAssistantError("Foxtrot has not reported this room's setpoint yet")
        steps = round((target - current) / STEP)
        if steps == 0:
            return
        low, high = limits({"heattemp": current, "delta": delta})
        if not low - 1e-6 <= current + steps * STEP <= high + 1e-6:
            raise HomeAssistantError(
                f"Foxtrot can only move this setpoint between {low:g} and {high:g} °C right now "
                f"(the program's temperature for the current mode, plus or minus {MAX_DELTA:g} K)"
            )
        field, sign = ("incdelta", 1) if steps > 0 else ("decdelta", -1)
        for done in range(1, abs(steps) + 1):
            wanted = delta + sign * done * STEP
            await coordinator.async_pulse(
                prefix, field, True, lambda: abs((value("delta") or 0.0) - wanted) < STEP / 2,
                f"change the setpoint to {current + sign * done * STEP:g} °C")
