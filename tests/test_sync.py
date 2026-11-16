"""A schedule helper is the master of a thermostat's weekly program; the options flow links them."""
from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import SupportsResponse
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.foxtrot_teco import const
from custom_components.foxtrot_teco.const import CONF_SCHEDULES, DOMAIN
from custom_components.foxtrot_teco.schedule import TimeProgram, week_from_helper
from fake_plccoms import FakePlccoms, TpwSim, variables_from_contract
from test_setup import CONTRACT, SERIAL, coordinator, fast, start, until  # noqa: F401

PREFIX = "main.livingroom.thermostat.timeprogcontrol"
OBYVAK = (Path(__file__).parent / "fixtures" / "schedules" / "WTP_1567.JSN").read_text(encoding="cp1250")
LINK = {"Obyvak": {"entity_id": "schedule.obyvak", "base_mode": "eco"}}
H = 3600

WEEK = {   # a schedule helper as get_schedule returns it
    "monday": [{"from": "06:00:00", "to": "08:00:00", "data": {"mode": "comfort"}},
               {"from": "17:00:00", "to": "23:00:00", "data": {"mode": "comfort"}}],
    "tuesday": [{"from": "06:00:00", "to": "08:00:00", "data": {"mode": "comfort"}}],
    "wednesday": [], "thursday": [], "friday": [], "saturday": [], "sunday": [],
}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(const, "PROGRAM_TIMEOUT", 0.5)
    monkeypatch.setattr(const, "TPW_TIMEOUT", 0.5)
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
def helper(hass):
    """A stand-in for the schedule helper: ``helper["schedule"]`` is what get_schedule answers."""
    state = {"schedule": WEEK}

    async def get_schedule(call):
        entities = call.data["entity_id"]
        return {e: state["schedule"] for e in ([entities] if isinstance(entities, str) else entities)}

    hass.services.async_register("schedule", "get_schedule", get_schedule, supports_response=SupportsResponse.ONLY)
    hass.states.async_set("schedule.obyvak", "off")
    return state


def make_entry(hass, plc, links):
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, unique_id=f"{DOMAIN}:127.0.0.1:{plc.port}",
        data={"host": "127.0.0.1", "port": plc.port, "serial": SERIAL},
        options={CONF_SCHEDULES: links} if links else {},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def entry(hass, plc, helper):
    entry = make_entry(hass, plc, LINK)
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def on_plc(plc) -> TimeProgram:
    return TimeProgram.loads(plc.files[plc.sim.file_key()])


def writes(plc):
    return [c for c in plc.commands if c.startswith("WRITEFILE") and "[0]=" not in c]


def issue(hass):
    return ir.async_get(hass).async_get_issue(DOMAIN, "schedule_not_sent_Obyvak")


async def test_the_schedule_becomes_the_program_of_the_thermostat(hass, entry, plc):
    await start(hass, entry)
    expected = week_from_helper(WEEK, "eco")
    await until(hass, lambda: on_plc(plc).week() == expected)
    program = on_plc(plc)
    assert program.heat_temp == TimeProgram.loads(OBYVAK).heat_temp        # temperatures are not the schedule's
    assert expected[0] == [(0, "eco"), (6 * H, "comfort"), (8 * H, "eco"), (17 * H, "comfort"), (23 * H, "eco")]
    assert issue(hass) is None


async def test_what_the_plc_already_runs_is_not_written_again(hass, entry, plc, helper):
    await start(hass, entry)
    await until(hass, lambda: len(writes(plc)) == 1)
    for _ in range(2):                                  # the same schedule reported, changed back and forth
        hass.states.async_set("schedule.obyvak", "on")
        hass.states.async_set("schedule.obyvak", "off")
        await until(hass, lambda: True)
    await hass.async_block_till_done()
    import asyncio
    await asyncio.sleep(0.3)
    assert len(writes(plc)) == 1


async def test_a_changed_schedule_is_sent_after_a_moment(hass, entry, plc, helper):
    await start(hass, entry)
    await until(hass, lambda: len(writes(plc)) == 1)
    helper["schedule"] = {**WEEK, "wednesday": [{"from": "05:00:00", "to": "07:00:00", "data": {"mode": "sleep"}}]}
    hass.states.async_set("schedule.obyvak", "off", {"edited": True})
    await until(hass, lambda: on_plc(plc).week()[2] == [(0, "eco"), (5 * H, "sleep"), (7 * H, "eco")])
    assert len(writes(plc)) == 2


async def test_an_edit_that_leaves_the_state_alone_is_noticed_by_the_report(hass, entry, plc, helper):
    await start(hass, entry)
    await until(hass, lambda: len(writes(plc)) == 1)
    helper["schedule"] = {**WEEK, "sunday": [{"from": "09:00:00", "to": "10:00:00", "data": {"mode": "comfort"}}]}
    hass.states.async_set("schedule.obyvak", "off")        # same state, same attributes: only "reported"
    await until(hass, lambda: on_plc(plc).week()[6] == [(0, "eco"), (9 * H, "comfort"), (10 * H, "eco")])


async def test_a_schedule_that_cannot_be_a_program_is_explained_and_nothing_is_written(hass, entry, plc, helper):
    helper["schedule"] = {**WEEK, "monday": [{"from": "06:00:00", "to": "08:00:00"}]}
    await start(hass, entry)
    await until(hass, lambda: issue(hass) is not None)
    assert "needs data mode" in issue(hass).translation_placeholders["reason"]
    assert not writes(plc)

    helper["schedule"] = WEEK
    hass.states.async_set("schedule.obyvak", "on")
    await until(hass, lambda: issue(hass) is None)
    assert len(writes(plc)) == 1


async def test_a_program_the_plc_does_not_load_is_reported(hass, entry, plc, helper):
    plc.sim.reject = True
    await start(hass, entry)
    await until(hass, lambda: issue(hass) is not None, timeout=5)
    assert "did not load" in issue(hass).translation_placeholders["reason"]
    assert entry.state is ConfigEntryState.LOADED


async def test_a_thermostat_without_a_link_is_never_written(hass, plc, helper):
    entry = make_entry(hass, plc, None)
    await start(hass, entry)
    import asyncio
    await asyncio.sleep(0.3)
    assert not writes(plc)
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("expected_lingering_timers", [True])      # the helper itself keeps a timer for its next block
async def test_the_real_schedule_helper_is_understood(hass, plc):
    assert await async_setup_component(hass, "schedule", {"schedule": {"obyvak": {
        "name": "Obyvak",
        "monday": [{"from": "06:00:00", "to": "08:00:00", "data": {"mode": "comfort"}}],
        "sunday": [{"from": "20:00:00", "to": "24:00:00", "data": {"mode": "sleep"}}],
    }}})
    entry = make_entry(hass, plc, LINK)
    await start(hass, entry)
    await until(hass, lambda: on_plc(plc).week()[6] == [(0, "eco"), (20 * H, "sleep")])
    assert on_plc(plc).week()[0] == [(0, "eco"), (6 * H, "comfort"), (8 * H, "eco")]
    await hass.config_entries.async_unload(entry.entry_id)
