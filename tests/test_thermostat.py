"""Presets and setpoint of a room thermostat, through the PLC's time-program block (what iFoxtrot does)."""
from __future__ import annotations

import copy

import pytest
from homeassistant.components.climate import (
    ATTR_PRESET_MODE, ATTR_TEMPERATURE, DOMAIN as CLIMATE, SERVICE_SET_HVAC_MODE, SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.foxtrot_teco import const, thermostat
from custom_components.foxtrot_teco.const import DOMAIN
from fake_plccoms import FakePlccoms, TpwSim, variables_from_contract
from test_setup import CLIMATE as LIVINGROOM, CONTRACT, SAMPLES, SERIAL, coordinator, entity_id, fast, start, until  # noqa: F401

PREFIX = "main.livingroom.thermostat.timeprogcontrol"
HEAT_TEMPS = (5.0, 19.0, 21.0, 23.0)         # limit, eco, night, day


@pytest.fixture(params=[0.0, 0.03], ids=["scan-at-once", "scan-later"])
def scan_delay(request):
    """0: the PLC consumes a command before the server polls (no DIFF shows it); else a scan later."""
    return request.param


@pytest.fixture
async def plc(scan_delay):
    fake = FakePlccoms(variables_from_contract(CONTRACT))
    sim = TpwSim(fake, PREFIX, "Obyvak", heat_temps=HEAT_TEMPS, schedule_mode=2, scan_delay=scan_delay)
    fake.set_variables({**fake.variables, **sim.variables()})
    fake.sim = sim
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def entry(hass, plc):
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, unique_id=f"{DOMAIN}:127.0.0.1:{plc.port}",
        data={"host": "127.0.0.1", "port": plc.port, "serial": SERIAL},
    )
    entry.add_to_hass(hass)
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def climate(hass, entry):
    return hass.states.get(entity_id(hass, entry, "climate", LIVINGROOM))


async def call(hass, entry, service, **data):
    await hass.services.async_call(
        CLIMATE, service, {ATTR_ENTITY_ID: entity_id(hass, entry, "climate", LIVINGROOM), **data}, blocking=True)


async def test_the_climate_object_is_linked_to_its_time_program(hass, entry, plc):
    await start(hass, entry)
    state = climate(hass, entry)
    assert state.attributes["preset_modes"] == ["schedule", "comfort", "sleep", "eco", "limit"]
    assert state.attributes["preset_mode"] == "schedule"
    assert state.attributes[ATTR_TEMPERATURE] == 21.0
    assert (state.attributes["min_temp"], state.attributes["max_temp"]) == (16.0, 26.0)
    assert state.attributes["target_temp_step"] == 0.5
    assert state.attributes["hvac_modes"] == ["auto"]
    assert state.attributes["holiday"] is False


@pytest.mark.parametrize("preset, mode", [("comfort", 3), ("sleep", 2), ("eco", 1), ("limit", 0)])
async def test_a_preset_switches_to_manual_in_that_mode(hass, entry, plc, preset, mode):
    await start(hass, entry)
    await call(hass, entry, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: preset})
    assert (plc.sim.manual, plc.sim.mode) == (True, mode)
    await until(hass, lambda: climate(hass, entry).attributes["preset_mode"] == preset)
    await until(hass, lambda: climate(hass, entry).attributes[ATTR_TEMPERATURE] == HEAT_TEMPS[mode])


async def test_the_schedule_preset_returns_to_auto(hass, entry, plc):
    await start(hass, entry)
    await call(hass, entry, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "eco"})
    await call(hass, entry, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "schedule"})
    assert (plc.sim.manual, plc.sim.mode) == (False, 2)
    await until(hass, lambda: climate(hass, entry).attributes["preset_mode"] == "schedule")
    assert climate(hass, entry).attributes[ATTR_TEMPERATURE] == 21.0


async def test_hvac_mode_auto_means_follow_the_schedule(hass, entry, plc):
    await start(hass, entry)
    await call(hass, entry, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "comfort"})
    await call(hass, entry, SERVICE_SET_HVAC_MODE, hvac_mode="auto")
    assert plc.sim.manual is False


async def test_a_preset_that_is_already_in_effect_sends_nothing(hass, entry, plc):
    await start(hass, entry)
    await call(hass, entry, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "schedule"})
    assert not [c for c in plc.commands if c.upper().startswith("SET:")]


async def test_the_mode_is_set_after_manual_not_with_it(hass, entry, plc):
    """In auto the program overwrites a mode in the same scan, so manual must be confirmed first."""
    await start(hass, entry)
    await call(hass, entry, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "comfort"})
    sets = [c.lower() for c in plc.commands if c.upper().startswith("SET:")]
    assert [s.split(",")[0] for s in sets] == [f"set:{PREFIX}.gtsap1_tpw_manualset", f"set:{PREFIX}.gtsap1_tpw_modeset"]


async def test_the_setpoint_moves_in_half_degree_steps(hass, entry, plc):
    await start(hass, entry)
    await call(hass, entry, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 22.5})
    assert plc.sim.delta == 1.5 and plc.sim.manual is True          # auto -> manual with the first step
    await until(hass, lambda: climate(hass, entry).attributes[ATTR_TEMPERATURE] == 22.5)
    state = climate(hass, entry)
    assert state.attributes["preset_mode"] == "sleep"                # the mode in effect (night) stays
    assert state.attributes["delta"] == 1.5
    assert (state.attributes["min_temp"], state.attributes["max_temp"]) == (16.0, 26.0)

    await call(hass, entry, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 20.0})
    assert plc.sim.delta == -1.0
    sets = [c.split(",")[0].lower() for c in plc.commands if c.upper().startswith("SET:")]
    assert sets.count(f"set:{PREFIX}.gtsap1_tpw_incdelta") == 3
    assert sets.count(f"set:{PREFIX}.gtsap1_tpw_decdelta") == 5


async def test_a_setpoint_the_delta_cannot_reach_is_refused_before_anything_is_sent(hass, entry, plc):
    await start(hass, entry)
    with pytest.raises(ServiceValidationError, match="16.0 to 26.0"):     # Home Assistant checks min/max_temp
        await call(hass, entry, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 30.0})
    # ... and so does the integration, should the limits have moved since (a mode change)
    with pytest.raises(HomeAssistantError, match="between 16 and 26"):
        await thermostat.set_target(coordinator(hass, entry), PREFIX, 30.0)
    assert not [c for c in plc.commands if c.upper().startswith("SET:")]


async def test_the_limits_follow_the_mode(hass, entry, plc):
    await start(hass, entry)
    await call(hass, entry, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "comfort"})
    await until(hass, lambda: climate(hass, entry).attributes["max_temp"] == 28.0)
    assert climate(hass, entry).attributes["min_temp"] == 18.0


async def test_a_manual_setting_ends_with_the_interval_of_the_program(hass, entry, plc):
    await start(hass, entry)
    await call(hass, entry, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "comfort"})
    plc.sim.next_interval(1)                                           # the weekly program moves on
    await until(hass, lambda: climate(hass, entry).attributes["preset_mode"] == "schedule")
    assert climate(hass, entry).attributes[ATTR_TEMPERATURE] == HEAT_TEMPS[1]


async def test_a_command_the_plc_ignores_is_reported(hass, entry, plc, monkeypatch):
    monkeypatch.setattr(const, "TPW_TIMEOUT", 0.3)
    await start(hass, entry)
    plc.ignore_set = True
    with pytest.raises(HomeAssistantError, match="did not switch to manual"):
        await call(hass, entry, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "comfort"})
    assert entry.state is ConfigEntryState.LOADED


async def test_a_refused_command_is_reported(hass, entry, plc):
    await start(hass, entry)
    plc.variables.pop(f"{PREFIX}.GTSAP1_TPW_MANUALSET".upper())
    with pytest.raises(HomeAssistantError, match="did not take the command"):
        await call(hass, entry, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "comfort"})


async def test_without_a_published_time_program_the_setpoint_is_written_as_before(hass, entry, plc):
    for name in [n for n in plc.variables if ".GTSAP1_TPW_" in n]:
        del plc.variables[name]
    await start(hass, entry)
    state = climate(hass, entry)
    assert "preset_modes" not in state.attributes or not state.attributes["preset_modes"]
    await call(hass, entry, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 19.5})
    assert plc.variables[f"{LIVINGROOM}.SP".upper()][1] == 19.5


async def test_two_programs_with_one_name_leave_the_object_unlinked(hass, entry, plc, caplog):
    twin = TpwSim(plc, "main.bedroom.thermostat.timeprogcontrol", "Obyvak")
    plc.variables.update({k.upper(): v for k, v in twin.variables().items()})
    await start(hass, entry)
    assert not climate(hass, entry).attributes.get("preset_modes")
    assert "does not publish exactly once" in caplog.text
