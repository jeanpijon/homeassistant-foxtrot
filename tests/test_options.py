"""Linking a thermostat's weekly program to a schedule helper in the options of the integration."""
from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.foxtrot_teco import const
from custom_components.foxtrot_teco.const import CONF_SCHEDULES, DOMAIN
from fake_plccoms import FakePlccoms, TpwSim, variables_from_contract
from test_setup import CONTRACT, SERIAL, fast, start, until  # noqa: F401

PREFIX = "main.livingroom.thermostat.timeprogcontrol"
OBYVAK = (Path(__file__).parent / "fixtures" / "schedules" / "WTP_1567.JSN").read_text(encoding="cp1250")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    # a schedule helper that does not exist makes every sync fail at once, which is fine here
    monkeypatch.setattr(const, "SYNC_DEBOUNCE", 0.05)


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
    await start(hass, entry)
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def settled(hass, entry):
    """The entry reloads when its options change; wait until that is over."""
    await hass.async_block_till_done()
    await until(hass, lambda: entry.state is ConfigEntryState.LOADED)
    await hass.async_block_till_done()


async def pick_room(hass, entry):
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.FORM and flow["step_id"] == "init"
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"room": "Obyvak"})
    assert flow["step_id"] == "room" and flow["description_placeholders"] == {"room": "Obyvak"}
    return flow


async def test_a_schedule_is_linked_after_the_overwrite_is_confirmed(hass, entry):
    flow = await pick_room(hass, entry)
    refused = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"schedule": "schedule.obyvak", "base_mode": "sleep"})
    assert refused["type"] is FlowResultType.FORM and refused["errors"] == {"confirm": "confirm_overwrite"}

    done = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"schedule": "schedule.obyvak", "base_mode": "sleep", "confirm": True})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCHEDULES] == {"Obyvak": {"entity_id": "schedule.obyvak", "base_mode": "sleep"}}
    await settled(hass, entry)                                              # the entry reloads with the link


async def test_a_link_that_stays_needs_no_new_confirmation_to_change_the_base_mode(hass, entry):
    hass.config_entries.async_update_entry(entry, options={CONF_SCHEDULES: {
        "Obyvak": {"entity_id": "schedule.obyvak", "base_mode": "eco"}}})
    await settled(hass, entry)
    flow = await pick_room(hass, entry)
    done = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"schedule": "schedule.obyvak", "base_mode": "limit"})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCHEDULES]["Obyvak"]["base_mode"] == "limit"
    await settled(hass, entry)


async def test_an_empty_schedule_removes_the_link(hass, entry):
    hass.config_entries.async_update_entry(entry, options={CONF_SCHEDULES: {
        "Obyvak": {"entity_id": "schedule.obyvak", "base_mode": "eco"}}})
    await settled(hass, entry)
    flow = await pick_room(hass, entry)
    done = await hass.config_entries.options.async_configure(flow["flow_id"], {"base_mode": "eco"})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCHEDULES] == {}
    await settled(hass, entry)


async def test_without_a_thermostat_with_a_program_there_is_nothing_to_link(hass, plc):
    for name in [n for n in plc.variables if ".GTSAP1_TPW_" in n]:
        del plc.variables[name]
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, unique_id=f"{DOMAIN}:127.0.0.1:{plc.port}",
        data={"host": "127.0.0.1", "port": plc.port, "serial": SERIAL},
    )
    entry.add_to_hass(hass)
    await start(hass, entry)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "no_thermostats"
    await hass.config_entries.async_unload(entry.entry_id)
