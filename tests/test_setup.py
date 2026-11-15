"""Power-outage behaviour: the PLC (or TecoApi) comes up after Home Assistant."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from aiohttp import ClientConnectionError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.foxtrot_teco import make_unique_id
from custom_components.foxtrot_teco.const import DOMAIN

FACE = {"ha_room": {"kind": 2, "name": "Room", "unit": "1", "value": 21.5, "key": "room"}}


class FakeTecoApi:
    """Stands in for TecoApiClient; behaviour is switched per test."""

    info: dict = {}
    names: list = []
    down = False

    def __init__(self, *args, **kwargs):
        pass

    async def get_info(self):
        if FakeTecoApi.down:
            raise ClientConnectionError("PLC not reachable")
        return FakeTecoApi.info

    async def get_list(self, prefix="ha_"):
        return FakeTecoApi.names

    async def get_object(self, name):
        return {name: dict(FACE[name])}


@pytest.fixture
async def entry(hass):
    FakeTecoApi.info = {"plc": {"serialNumber": "M5 0075"}}
    FakeTecoApi.names = ["ha_room"]
    FakeTecoApi.down = False
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "http://192.0.2.9", "auth_type": "basic"})
    entry.add_to_hass(hass)
    with patch("custom_components.foxtrot_teco.TecoApiClient", FakeTecoApi):
        yield entry


async def start(hass, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def retry(hass):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=3))
    await hass.async_block_till_done(wait_background_tasks=True)


def unique_ids(hass, entry):
    return {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


async def test_plc_down_at_start_is_retried(hass, entry):
    FakeTecoApi.down = True
    await start(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    FakeTecoApi.down = False
    await retry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert unique_ids(hass, entry) == {make_unique_id("M5 0075", 2, "room", "ha_room")}


async def test_http_up_but_plc_program_not_running_does_not_load_empty(hass, entry):
    FakeTecoApi.names = []
    await start(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not unique_ids(hass, entry)

    FakeTecoApi.names = ["ha_room"]
    await retry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert len(unique_ids(hass, entry)) == 1


async def test_missing_serial_does_not_create_entities_under_a_placeholder(hass, entry):
    FakeTecoApi.info = {"plc": {}}
    await start(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    FakeTecoApi.info = {"plc": {"serialNumber": "M5 0075"}}
    await retry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert unique_ids(hass, entry) == {make_unique_id("M5 0075", 2, "room", "ha_room")}
    assert make_unique_id("unknown", 2, "room", "ha_room") not in unique_ids(hass, entry)
