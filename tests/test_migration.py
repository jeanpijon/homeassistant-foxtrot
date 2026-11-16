"""1.x (TecoApi over HTTP) entries must come through the move to PLCComS with entities intact."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.foxtrot_teco as integration
from custom_components.foxtrot_teco import make_unique_id
from custom_components.foxtrot_teco.const import DOMAIN
from fake_plccoms import FakePlccoms, variables_from_contract

CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "ha_contract.json").read_text())
SAMPLES = CONTRACT["samples"]
SERIAL = "M5 0075"
OLD_DATA = {"host": "http://192.168.11.11", "username": "test", "password": "secret",
            "auth_type": "digest", "verify_ssl": False}


def test_unique_id_is_stable_and_distinguishes_inputs():
    a = make_unique_id("123", 1, "room", "ha_room")
    assert a == make_unique_id("123", 1, "room", "ha_room")
    assert a != make_unique_id("124", 1, "room", "ha_room")
    assert a != make_unique_id("123", 2, "room", "ha_room")
    assert a.startswith("teco_") and len(a) == len("teco_") + 20


@pytest.fixture
async def plc(monkeypatch):
    fake = FakePlccoms(variables_from_contract(CONTRACT))
    await fake.start()
    monkeypatch.setattr(integration, "DEFAULT_PORT", fake.port)      # stands in for 5010
    yield fake
    await fake.stop()


def old_entry(hass, data=OLD_DATA):
    entry = MockConfigEntry(domain=DOMAIN, version=1, unique_id="foxtrot_teco:http://192.168.11.11", data=dict(data))
    entry.add_to_hass(hass)
    return entry


async def test_serial_entities_and_device_survive_and_credentials_are_gone(hass, plc):
    entry = old_entry(hass, {**OLD_DATA, "host": "http://127.0.0.1"})
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    # What 1.x registered: the stray "unknown" device from an early start, and the real one.
    devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "unknown")})
    device = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, SERIAL)},
                                         name=f"Foxtrot {SERIAL}")
    old_ids = {}
    for var, face in SAMPLES.items():
        domain = {1: "climate", 2: "sensor", 3: "binary_sensor"}[face["kind"]]
        uid = make_unique_id(SERIAL, face["kind"], face["key"], var)
        old_ids[uid] = entities.async_get_or_create(domain, DOMAIN, uid, config_entry=entry,
                                                    device_id=device.id).entity_id

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2
    assert entry.data == {"host": "127.0.0.1", "port": plc.port, "serial": SERIAL}
    assert entry.unique_id == f"{DOMAIN}:127.0.0.1:{plc.port}"
    registered = er.async_entries_for_config_entry(entities, entry.entry_id)
    assert {e.unique_id: e.entity_id for e in registered} == old_ids          # nothing new, nothing lost
    assert all(hass.states.get(eid).state != "unavailable" for eid in old_ids.values())

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_without_a_registered_device_the_host_stands_in_for_the_serial(hass, plc):
    entry = old_entry(hass, {**OLD_DATA, "host": "127.0.0.1"})                # no scheme
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data["serial"] == "127.0.0.1"
    assert entry.data["host"] == "127.0.0.1"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_entry_without_a_host_cannot_be_migrated(hass, plc):
    entry = old_entry(hass, {"username": "test"})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
