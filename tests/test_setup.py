"""The integration against a fake PLCComS server: start-up, pushes, outages, writes."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.foxtrot_teco import const, make_unique_id, plccoms
from custom_components.foxtrot_teco.const import DOMAIN
from fake_plccoms import FakePlccoms, variables_from_contract

CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "ha_contract.json").read_text())
SAMPLES = CONTRACT["samples"]
SERIAL = "M5 0075"
AIR = "ha_livingroom_temp_air"
CLIMATE = "ha_livingroom_climate"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    """Seconds become milliseconds."""
    monkeypatch.setattr(const, "PING_INTERVAL", 0.2)
    monkeypatch.setattr(const, "RECONNECT_MIN", 0.05)
    monkeypatch.setattr(const, "RECONNECT_MAX", 0.2)
    monkeypatch.setattr(const, "FLUSH_DELAY", 0.01)
    monkeypatch.setattr(plccoms, "COMMAND_TIMEOUT", 0.3)
    monkeypatch.setattr(plccoms, "SET_CONFIRM_TIMEOUT", 0.2)


@pytest.fixture
async def plc():
    samples = copy.deepcopy(SAMPLES)
    samples["ha_garage_light"] = {"name": "", "kind": 3, "state": False, "writable": True, "key": "garage_light"}
    fake = FakePlccoms(variables_from_contract(CONTRACT, samples))
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


async def start(hass, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def retry(hass, entry):
    """Let HA's set-up retry fire. (The link supervisor is a background task that never ends,
    so this cannot wait for background tasks.)"""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=3))
    await until(hass, lambda: entry.state is ConfigEntryState.LOADED)


async def until(hass, check, timeout=3.0):
    """Wait for something the supervisor does in the background."""
    for _ in range(int(timeout / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
        await hass.async_block_till_done()
    raise AssertionError("timed out")


def entity_id(hass, entry, domain, var):
    registry = er.async_get(hass)
    kind = {"sensor": 2, "binary_sensor": 3, "switch": 3, "climate": 1}[domain]
    key = SAMPLES.get(var, {}).get("key") or var
    if var == "ha_garage_light":
        key = "garage_light"
    return registry.async_get_entity_id(domain, DOMAIN, make_unique_id(SERIAL, kind, key, var))


def state_of(hass, entry, domain, var):
    eid = entity_id(hass, entry, domain, var)
    return hass.states.get(eid) if eid else None


def unique_ids(hass, entry):
    return {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


def coordinator(hass, entry):
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]


async def test_every_object_of_the_contract_becomes_an_entity(hass, entry):
    await start(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    expected = {
        make_unique_id(SERIAL, face["kind"], face.get("key") or var, var)
        for var, face in SAMPLES.items()
    } | {make_unique_id(SERIAL, 3, "garage_light", "ha_garage_light")}
    assert unique_ids(hass, entry) == expected
    assert float(state_of(hass, entry, "sensor", AIR).state) == SAMPLES[AIR]["value"]
    assert state_of(hass, entry, "binary_sensor", "ha_livingroom_valves").state == "on"
    assert state_of(hass, entry, "switch", "ha_garage_light").state == "off"


async def test_the_plc_pushes_changes_there_is_no_polling(hass, entry, plc):
    await start(hass, entry)
    plc.push(f"{AIR}.value", 25.0)
    plc.push("ha_garage_light.state", True)
    await until(hass, lambda: state_of(hass, entry, "sensor", AIR).state == "25.0")
    await until(hass, lambda: state_of(hass, entry, "switch", "ha_garage_light").state == "on")
    # Values are read once, at start-up; after that they only arrive by push.
    assert len([c for c in plc.commands if c.upper() == "GET:HA_*"]) == 1


async def test_a_plc_that_is_not_up_at_start_is_retried(hass, entry, plc):
    port = plc.port
    await plc.stop()
    await start(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    await plc.start(port)
    await retry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert len(unique_ids(hass, entry)) == len(SAMPLES) + 1


async def test_plccoms_up_but_the_plc_program_not_running_does_not_load_empty(hass, entry, plc):
    full = dict(plc.variables)
    plc.set_variables({})
    await start(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not unique_ids(hass, entry)

    plc.variables = full
    await retry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert len(unique_ids(hass, entry)) == len(SAMPLES) + 1


async def test_a_halted_plc_does_not_load(hass, entry, plc):
    plc.variables["__PLC_RUN"] = ("BOOL", False)
    await start(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    plc.variables["__PLC_RUN"] = ("BOOL", True)
    await retry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED


async def test_an_object_without_a_kind_is_ignored(hass, entry, plc):
    plc.variables["HA_STRAY.VALUE"] = ("REAL", 1.0)
    await start(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert len(unique_ids(hass, entry)) == len(SAMPLES) + 1


async def test_entities_go_unavailable_when_the_link_drops_and_catch_up_after(hass, entry, plc):
    await start(hass, entry)
    assert state_of(hass, entry, "sensor", AIR).state == "21.5"

    await plc.drop()
    plc.variables["HA_LIVINGROOM_TEMP_AIR.VALUE"] = ("REAL", 18.0)   # changed while we were away
    await until(hass, lambda: state_of(hass, entry, "sensor", AIR).state == "18.0")
    assert entry.state is ConfigEntryState.LOADED


async def test_a_silent_plc_is_noticed_and_forgiven(hass, entry, plc):
    await start(hass, entry)
    plc.mute = True
    await until(hass, lambda: state_of(hass, entry, "sensor", AIR).state == "unavailable")
    plc.mute = False
    await until(hass, lambda: state_of(hass, entry, "sensor", AIR).state == "21.5")


async def test_a_stopping_plc_program_makes_entities_unavailable(hass, entry, plc):
    await start(hass, entry)
    plc.push("__PLC_RUN", False)
    await until(hass, lambda: state_of(hass, entry, "sensor", AIR).state == "unavailable")
    plc.push("__PLC_RUN", True)
    await until(hass, lambda: state_of(hass, entry, "sensor", AIR).state == "21.5")


async def test_a_reloaded_program_with_the_same_objects_is_reread_without_a_reload(hass, entry, plc):
    await start(hass, entry)
    before = coordinator(hass, entry)
    await plc.replace_program(dict(plc.variables))           # clients lose what they enabled
    await until(hass, lambda: plc.conns and any(c.enabled for c in plc.conns))
    plc.push(f"{AIR}.value", 26.0)
    await until(hass, lambda: state_of(hass, entry, "sensor", AIR).state == "26.0")
    assert coordinator(hass, entry) is before


async def test_a_reloaded_program_with_new_objects_reloads_the_entry(hass, entry, plc):
    await start(hass, entry)
    before = coordinator(hass, entry)
    variables = dict(plc.variables)
    variables.update({
        "ha_cellar_temp_air.kind": ("INT", 2), "ha_cellar_temp_air.unit": ("STRING[16]", "1"),
        "ha_cellar_temp_air.value": ("REAL", 12.0), "ha_cellar_temp_air.key": ("STRING[64]", ""),
        "ha_cellar_temp_air.name": ("STRING[80]", ""),
    })
    await plc.replace_program({k: v for k, v in variables.items() if not k.startswith("__")})
    await until(hass, lambda: entity_id(hass, entry, "sensor", "ha_cellar_temp_air"))
    assert coordinator(hass, entry) is not before
    assert entry.state is ConfigEntryState.LOADED


async def test_a_program_that_drops_objects_reloads_the_entry(hass, entry, plc):
    await start(hass, entry)
    before = coordinator(hass, entry)
    assert "ha_livingroom_co2" in before.data
    await plc.replace_program({k: v for k, v in plc.variables.items() if "LIVINGROOM_CO2" not in k})
    await until(hass, lambda: coordinator(hass, entry) is not before)
    await until(hass, lambda: entry.state is ConfigEntryState.LOADED)
    assert "ha_livingroom_co2" not in coordinator(hass, entry).data


async def test_a_refresh_while_connected_rereads_without_reconnecting(hass, entry, plc):
    await start(hass, entry)
    assert plc.connections_made == 1
    await coordinator(hass, entry).async_refresh()
    assert plc.connections_made == 1
    plc.push(f"{AIR}.value", 27.0)                    # the pushes still come
    await until(hass, lambda: state_of(hass, entry, "sensor", AIR).state == "27.0")


async def test_a_write_that_finds_the_link_dead_makes_the_entities_unavailable_at_once(hass, entry, plc, monkeypatch):
    monkeypatch.setattr(const, "PING_INTERVAL", 30)    # so only the failed write can have noticed
    await start(hass, entry)
    plc.mute = True
    eid = entity_id(hass, entry, "switch", "ha_garage_light")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("switch", "turn_on", {"entity_id": eid}, blocking=True)
    await until(hass, lambda: hass.states.get(eid).state == "unavailable", timeout=2)


async def test_a_setpoint_written_in_home_assistant_reaches_the_plc(hass, entry, plc):
    await start(hass, entry)
    eid = entity_id(hass, entry, "climate", CLIMATE)
    await hass.services.async_call("climate", "set_temperature", {"entity_id": eid, "temperature": 23.5}, blocking=True)
    assert plc.variables["HA_LIVINGROOM_CLIMATE.SP"][1] == 23.5
    await until(hass, lambda: hass.states.get(eid).attributes["temperature"] == 23.5)


async def test_a_switch_is_written_to_the_plc(hass, entry, plc):
    await start(hass, entry)
    eid = entity_id(hass, entry, "switch", "ha_garage_light")
    await hass.services.async_call("switch", "turn_on", {"entity_id": eid}, blocking=True)
    assert plc.variables["HA_GARAGE_LIGHT.STATE"][1] is True
    await until(hass, lambda: hass.states.get(eid).state == "on")
    await hass.services.async_call("switch", "turn_off", {"entity_id": eid}, blocking=True)
    await until(hass, lambda: hass.states.get(eid).state == "off")


async def test_a_write_the_plc_refuses_is_an_error(hass, entry, plc):
    await start(hass, entry)
    plc.ignore_set = True
    eid = entity_id(hass, entry, "switch", "ha_garage_light")
    with pytest.raises(HomeAssistantError, match="did not take"):
        await hass.services.async_call("switch", "turn_on", {"entity_id": eid}, blocking=True)


async def test_unloading_closes_the_connection(hass, entry, plc):
    await start(hass, entry)
    await until(hass, lambda: len(plc.conns) == 1)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await until(hass, lambda: not plc.conns)
