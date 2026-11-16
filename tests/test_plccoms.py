"""PLCComS client: the protocol as observed on the PLC, against a fake server (fake_plccoms.py)."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.foxtrot_teco import plccoms
from custom_components.foxtrot_teco.plccoms import (
    PlccomsClient,
    PlccomsCommandError,
    PlccomsConnectionError,
    PlccomsError,
    convert,
    format_value,
)
from fake_plccoms import FakePlccoms

VARS = {
    "ha_room.name": ("STRING[80]", "Room"),
    "ha_room.kind": ("INT", 2),
    "ha_room.unit": ("STRING[16]", "1"),
    "ha_room.value": ("REAL", 21.5),
    "ha_valve.kind": ("INT", 3),
    "ha_valve.state": ("BOOL", True),
    "ha_valve.writable": ("BOOL", True),
    "other_variable": ("INT", 7),
}


@pytest.fixture
async def plc():
    fake = FakePlccoms(VARS)
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def seen():
    return {"changes": [], "reloads": 0, "disconnects": 0}


@pytest.fixture
async def client(plc, seen):
    c = PlccomsClient(
        "127.0.0.1",
        plc.port,
        on_change=seen["changes"].append,
        on_reload=lambda: seen.__setitem__("reloads", seen["reloads"] + 1),
        on_disconnect=lambda: seen.__setitem__("disconnects", seen["disconnects"] + 1),
    )
    await c.connect()
    yield c
    await c.close()


@pytest.mark.parametrize(
    ("raw", "plc_type", "expected"),
    [
        ('"Obyvak"', "STRING[24]", "Obyvak"),
        ('"a,b"', "STRING[24]", "a,b"),
        ("1", "BOOL", True),
        ("0", "BOOL", False),
        ("23.900000", "REAL", 23.9),
        ("-3", "SINT", -3),
        ("3306594901", "DWORD", 3306594901),
        ("5", None, 5),
        ("5.5", None, 5.5),
        ('"x"', None, "x"),
        ("garbage", "REAL", "garbage"),            # kept as text rather than crashing the reader
    ],
)
def test_values_follow_the_plc_type(raw, plc_type, expected):
    assert convert(raw, plc_type) == expected


@pytest.mark.parametrize(
    ("value", "plc_type", "expected"),
    [(True, "BOOL", "1"), (False, "BOOL", "0"), (22.5, "REAL", "22.5"), (3, "INT", "3"), ("hi", "STRING[8]", '"hi"')],
)
def test_values_are_written_the_way_the_plc_reads_them(value, plc_type, expected):
    assert format_value(value, plc_type) == expected


async def test_list_gives_names_and_types_without_the_disabled_marker(client):
    found = await client.list_variables("ha_*")
    assert found["ha_room.value"] == "REAL"
    assert found["ha_valve.state"] == "BOOL"
    assert "other_variable" not in found
    assert all(name == name.lower() for name in found)


async def test_what_the_plc_stops_listing_is_forgotten(client, plc):
    await client.list_variables("ha_*")
    await client.enable("ha_*")
    await client.snapshot("ha_*")
    assert "ha_valve.state" in client.values

    await plc.replace_program({k: v for k, v in VARS.items() if not k.startswith("ha_valve")})
    await asyncio.sleep(0.05)
    assert "ha_valve.state" not in await client.list_variables("ha_*")
    assert "ha_valve.state" not in client.values and "ha_valve.state" not in client.types
    assert "ha_room.value" in client.types


async def test_a_wildcard_read_returns_only_enabled_variables(client):
    await client.list_variables("ha_*")
    assert await client.snapshot("ha_*") == []

    await client.enable("ha_*")
    names = await client.snapshot("ha_*")
    assert sorted(names) == sorted(n for n in VARS if n.startswith("ha_"))
    assert client.values["ha_room.name"] == "Room"
    assert client.values["ha_room.kind"] == 2
    assert client.values["ha_room.value"] == 21.5
    assert client.values["ha_valve.state"] is True


async def test_changes_are_pushed_and_reals_only_beyond_the_delta(client, plc, seen):
    await client.list_variables("ha_*")
    await client.enable("ha_room.value", 0.1)
    await client.enable("ha_valve.state")
    await client.snapshot("ha_*")
    seen["changes"].clear()

    plc.push("ha_room.value", 21.55)              # within 0.1
    plc.push("ha_valve.state", False)
    plc.push("ha_room.value", 22.0)
    await asyncio.sleep(0.05)

    assert seen["changes"] == ["ha_valve.state", "ha_room.value"]
    assert client.values["ha_valve.state"] is False
    assert client.values["ha_room.value"] == 22.0


async def test_a_push_in_the_middle_of_a_wildcard_answer_is_not_taken_for_its_end(client, plc):
    await client.list_variables("ha_*")
    await client.enable("ha_*")
    plc.inject = [("ha_room.value", 19.0)]
    names = await client.snapshot("ha_*")
    assert len(names) == len([n for n in VARS if n.startswith("ha_")])
    assert client.values["ha_room.value"] == 19.0


async def test_commands_wait_for_each_other(client):
    pf, listed, version = await asyncio.gather(
        client.get("__plc_run"), client.list_variables("ha_*"), client.get_info("version_plc")
    )
    assert pf is True
    assert "ha_room.kind" in listed
    assert version.startswith("CP2000I")


async def test_set_is_confirmed_by_the_pushed_change(client, plc):
    await client.list_variables("ha_*")
    await client.enable("ha_*")
    assert await client.set("ha_valve.state", False) is False
    assert plc.variables["HA_VALVE.STATE"][1] is False


async def test_set_without_a_push_is_checked_by_reading_back(client, plc, monkeypatch):
    monkeypatch.setattr(plccoms, "SET_CONFIRM_TIMEOUT", 0.2)
    await client.list_variables("ha_*")
    await client.enable("ha_*")
    plc.swallow_diff_on_set = True
    assert await client.set("ha_room.value", 23.5) == 23.5
    assert client.connected                       # silence after a SET is normal, not a dead link


async def test_a_write_the_plc_does_not_take_is_an_error(client, plc, monkeypatch):
    monkeypatch.setattr(plccoms, "SET_CONFIRM_TIMEOUT", 0.2)
    await client.list_variables("ha_*")
    await client.enable("ha_*")
    plc.ignore_set = True
    with pytest.raises(PlccomsError, match="stays"):
        await client.set("ha_room.value", 30.0)
    assert client.connected


async def test_unknown_names_are_command_errors(client):
    with pytest.raises(PlccomsCommandError) as err:
        await client.get("nope")
    assert err.value.code == 33
    with pytest.raises(PlccomsCommandError):
        await client.set("nope", 1)
    assert client.connected                       # the answer was an error, not silence


async def test_a_silent_plc_costs_the_connection_and_the_owner_hears_of_it(client, plc, seen, monkeypatch):
    monkeypatch.setattr(plccoms, "COMMAND_TIMEOUT", 0.2)
    plc.mute = True
    with pytest.raises(PlccomsConnectionError):
        await client.get("__plc_run")
    assert not client.connected
    assert seen["disconnects"] == 1


async def test_losing_the_connection_is_reported_and_fails_what_waits(client, plc, seen):
    await plc.drop()
    await asyncio.sleep(0.05)
    assert seen["disconnects"] == 1
    assert not client.connected
    with pytest.raises(PlccomsConnectionError):
        await client.get("__plc_run")


async def test_a_new_program_in_the_plc_is_reported(client, plc, seen):
    await plc.replace_program(VARS)
    await asyncio.sleep(0.05)
    assert seen["reloads"] == 1


async def test_nobody_listening_is_a_connection_error():
    c = PlccomsClient("127.0.0.1", 1)
    with pytest.raises(PlccomsConnectionError):
        await c.connect()
