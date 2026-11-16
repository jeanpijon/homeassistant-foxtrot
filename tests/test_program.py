"""The weekly program of a thermostat as entities (mode temperatures, holiday) and its write path."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.components.date import ATTR_DATE, DOMAIN as DATE, SERVICE_SET_VALUE as SET_DATE
from homeassistant.components.number import ATTR_VALUE, DOMAIN as NUMBER, SERVICE_SET_VALUE as SET_NUMBER
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.foxtrot_teco import const, make_unique_id
from custom_components.foxtrot_teco.const import DOMAIN
from custom_components.foxtrot_teco.schedule import TimeProgram
from fake_plccoms import FakePlccoms, TpwSim, variables_from_contract
from test_setup import CONTRACT, SERIAL, coordinator, fast, start, until  # noqa: F401

PREFIX = "main.livingroom.thermostat.timeprogcontrol"
OBYVAK = (Path(__file__).parent / "fixtures" / "schedules" / "WTP_1567.JSN").read_text(encoding="cp1250")
MODES = ["limit", "eco", "sleep", "comfort"]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(const, "PROGRAM_TIMEOUT", 1.0)
    monkeypatch.setattr(const, "TPW_TIMEOUT", 0.5)


@pytest.fixture
async def plc():
    fake = FakePlccoms(variables_from_contract(CONTRACT))
    fake.sim = TpwSim(fake, PREFIX, "Obyvak", program=OBYVAK)
    fake.set_variables({**fake.variables, **fake.sim.variables()})
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


def temperature(hass, entry, mode):
    unique = make_unique_id(SERIAL, 1, f"livingroom_thermostat:heat:{mode}", "ha_livingroom_climate")
    entity_id = er.async_get(hass).async_get_entity_id(NUMBER, DOMAIN, unique)
    return entity_id, hass.states.get(entity_id)


def holiday(hass, entry, end):
    unique = make_unique_id(SERIAL, 1, f"livingroom_thermostat:holiday:{end}", "ha_livingroom_climate")
    entity_id = er.async_get(hass).async_get_entity_id(DATE, DOMAIN, unique)
    return entity_id, hass.states.get(entity_id)


def on_plc(plc) -> TimeProgram:
    return TimeProgram.loads(plc.files[plc.sim.file_key()])


async def test_the_programs_are_read_when_the_integration_starts(hass, entry, plc):
    await start(hass, entry)
    assert [float(temperature(hass, entry, m)[1].state) for m in range(4)] == [23.0, 23.0, 23.0, 24.0]
    assert temperature(hass, entry, 3)[1].name == "Foxtrot M5 0075 Obyvak comfort temperature"
    assert holiday(hass, entry, 0)[1].state == "unknown"                  # 1970-01-01 is "none"
    assert holiday(hass, entry, 1)[1].state == "2024-04-12"


async def test_a_new_mode_temperature_is_loaded_by_the_plc_and_shown(hass, entry, plc):
    await start(hass, entry)
    entity_id, _ = temperature(hass, entry, 1)
    await hass.services.async_call(NUMBER, SET_NUMBER, {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 21.5}, blocking=True)
    assert on_plc(plc).heat_temp == (23.0, 21.5, 23.0, 24.0)
    assert on_plc(plc).week() == TimeProgram.loads(OBYVAK).week()          # the rest is untouched
    await until(hass, lambda: float(temperature(hass, entry, 1)[1].state) == 21.5)
    # the app's update command: the file was left as .APP and the command was given
    assert plc.sim.file_key("APP") not in plc.files
    assert any(c.lower().startswith(f"set:{PREFIX}.gtsap1_tpw_update") for c in plc.commands)


async def test_a_holiday_is_written_into_the_program(hass, entry, plc):
    await start(hass, entry)
    entity_id, _ = holiday(hass, entry, 0)
    await hass.services.async_call(DATE, SET_DATE, {ATTR_ENTITY_ID: entity_id, ATTR_DATE: "2026-12-20"}, blocking=True)
    assert on_plc(plc).holiday == ("2026-12-20-00:00:0", "2024-04-12-00:00:0")
    await until(hass, lambda: holiday(hass, entry, 0)[1].state == "2026-12-20")


async def test_a_program_the_plc_does_not_take_is_an_error_and_changes_nothing_shown(hass, entry, plc):
    await start(hass, entry)
    plc.sim.reject = True                       # the block ignores what it is given
    entity_id, _ = temperature(hass, entry, 3)
    with pytest.raises(HomeAssistantError, match="did not load the new weekly program"):
        await hass.services.async_call(NUMBER, SET_NUMBER, {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 30.0}, blocking=True)
    assert float(temperature(hass, entry, 3)[1].state) == 24.0


async def test_a_program_that_cannot_be_read_leaves_the_entities_unavailable(hass, entry, plc):
    del plc.files[plc.sim.file_key()]
    await start(hass, entry)
    assert temperature(hass, entry, 3)[1].state == "unavailable"


async def test_a_program_changed_by_someone_else_is_read_again(hass, entry, plc):
    await start(hass, entry)
    changed = TimeProgram.loads(OBYVAK)
    from dataclasses import replace
    text = replace(changed, heat_temp=(22.0, 22.0, 22.0, 25.0)).dumps()
    plc.files[plc.sim.file_key()] = text
    plc.push(f"{PREFIX}.GTSAP1_TPW_CRC", 4321)                          # the app saved it: the crc moves
    await until(hass, lambda: float(temperature(hass, entry, 3)[1].state) == 25.0)


async def test_a_program_changed_while_the_link_was_down_is_read_again_afterwards(hass, entry, plc):
    from dataclasses import replace
    await start(hass, entry)
    assert float(temperature(hass, entry, 3)[1].state) == 24.0
    await plc.drop()
    plc.files[plc.sim.file_key()] = replace(TimeProgram.loads(OBYVAK), heat_temp=(21.0, 21.0, 21.0, 26.0)).dumps()
    plc.variables[f"{PREFIX}.GTSAP1_TPW_CRC".upper()] = ("UINT", 999)         # no push: nobody was listening
    await until(hass, lambda: temperature(hass, entry, 3)[1].state == "26.0")      # unavailable while away
