"""Adding the PLC in the UI."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType

from custom_components.foxtrot_teco.const import DOMAIN
from fake_plccoms import FakePlccoms, variables_from_contract

CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "ha_contract.json").read_text())


@pytest.fixture
async def plc():
    fake = FakePlccoms(variables_from_contract(CONTRACT))
    await fake.start()
    yield fake
    await fake.stop()


async def configure(hass, host, port):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"host": host, "port": port})


async def test_a_reachable_plc_with_ha_objects_is_added(hass, plc):
    result = await configure(hass, "127.0.0.1", plc.port)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Foxtrot TECO (CP2000I M 2.7 2.0)"
    # A new installation is identified by its address: PLCComS cannot tell the PLC's serial number.
    assert result["data"] == {"host": "127.0.0.1", "port": plc.port, "serial": "127.0.0.1"}
    entry = result["result"]
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_nobody_answering_is_cannot_connect(hass, plc):
    port = plc.port
    await plc.stop()
    result = await configure(hass, "127.0.0.1", port)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_plccoms_without_the_ha_variables_says_so(hass, plc):
    plc.set_variables({})
    result = await configure(hass, "127.0.0.1", plc.port)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_ha_objects"}
    await hass.async_block_till_done()
    await asyncio.sleep(0.05)
    assert not plc.conns                              # the probe connection was closed again
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_the_same_plc_cannot_be_added_twice(hass, plc):
    first = await configure(hass, "127.0.0.1", plc.port)
    assert first["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    second = await configure(hass, "127.0.0.1", plc.port)
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"
    assert await hass.config_entries.async_unload(first["result"].entry_id)
    await hass.async_block_till_done()
