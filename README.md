# Foxtrot TECO for Home Assistant

A Home Assistant integration for **TECO Foxtrot** PLCs (tested with a CP-2000). It talks to the PLC's own **TecoApi** over HTTP and turns the PLC variables that the PLC program publishes under an `ha_` name into Home Assistant entities.

It is the Home Assistant half of a two-part interface: **without a PLC program that publishes `ha_*` objects it has nothing to show.** All control logic (heating, valves, schedules) stays in the PLC; Home Assistant mirrors values and can send a few writes back.

## Requirements

- A Foxtrot PLC reachable from Home Assistant over HTTP, with **TecoApi enabled** and a user account for it. *To confirm: the exact steps to enable TecoApi and create the user on the controller; they are not documented here yet.*
- A PLC program that publishes objects of the three types below, with names starting `ha_`.
- Home Assistant 2024.12 or newer.

## Installation

**HACS (recommended):** HACS, three-dot menu, *Custom repositories*, add `https://github.com/jeanpijon/homeassistant-foxtrot` with category *Integration*, download, restart Home Assistant.

**Manual:** copy `custom_components/foxtrot_teco` into your Home Assistant `config/custom_components/` and restart.

## Setup

*Settings, Devices & services, Add integration, "Foxtrot TECO"*.

| Field | Meaning |
|---|---|
| PLC URL | e.g. `http://192.168.11.11` (the TecoApi lives under `/TecoApi/`) |
| Username, password | the TecoApi user |
| Auth type | `digest` (default), `basic` or `auto`. `basic` tries basic auth first; the other two try digest first. Every mode retries once with the other method when the PLC answers 401. |
| Verify SSL | off by default; only matters for `https://` URLs |

Use a dedicated TecoApi user, not a shared one: the login is stored in Home Assistant's config entry.

## What becomes an entity

On setup (and again on reload) the integration lists the PLC variables whose names start with `ha_` and reads each one. The `kind` field of the object decides what it is:

| `kind` | PLC type | Home Assistant entity |
|---|---|---|
| 1 | `T_HA_Climate` | `climate`: current temperature `pv`, target `sp`, action from `mode` |
| 2 | `T_HA_Sensor` | `sensor`: `value`, with device class and unit from the `unit` code |
| 3, `writable` false | `T_HA_Binary` | `binary_sensor`: `state` |
| 3, `writable` true | `T_HA_Binary` | `switch`: `state` (on/off writes `state`) |

Sensor `unit` codes: `0` none, `1` °C (temperature), `2` % (humidity), `3` ppm (CO2).

Entities hang under one device per PLC (named `Foxtrot <PLC serial>`). The entity name is the object's `name` if the PLC sets one, otherwise the variable name, so entity ids look like `sensor.foxtrot_m5_0075_ha_livingroom_temp_air`. The unique id is a hash of PLC serial, kind and the object's `key` (or its variable name if `key` is empty): **keep `key` stable in the PLC program, changing it creates a new entity.**

## How it behaves

- **Polling**, every 5 seconds, one HTTP request per `ha_` object (sequentially). There is no push.
- **Discovery happens at setup.** A new `ha_` object in the PLC program shows up after reloading the integration.
- **Late PLC:** if the PLC, its web server or its program is not up when Home Assistant starts (no answer, no PLC serial, or no `ha_*` objects yet), setup fails and Home Assistant retries by itself, with back-off up to 10 minutes. It never loads a half-empty entity set.
- If the PLC stops answering later, the entities become unavailable and recover on their own.

## Limitations

- Only objects named `ha_*` are visible. Anything else in the PLC is out of scope.
- `climate`: the HVAC mode is fixed to *auto*; Home Assistant cannot switch it. `mode` from the PLC is read as the **current action** (0 idle, 1 heating, 2 cooling), which is what the reference PLC program writes, although the PLC type comment describes it as a setting.
- `climate` setpoint: Home Assistant writes `sp`. Whether the value sticks depends on the PLC program: in the author's program the thermostat block assigns `sp` from its own schedule on every scan, so a written value may be overwritten. *To confirm on the controller.*
- One PLC per config entry; no YAML configuration (the old `tecoapi:` block of the upstream project is gone).

## The PLC side (the contract)

The integration reads exactly these fields; the names and types come from the PLC program's type definitions:

| Type | Fields (PLC type) |
|---|---|
| `T_HA_Sensor` (`kind` 2) | `name` STRING, `unit` STRING, `kind` INT, `value` REAL, `key` STRING |
| `T_HA_Binary` (`kind` 3) | `name` STRING, `kind` INT, `state` BOOL, `writable` BOOL, `key` STRING |
| `T_HA_Climate` (`kind` 1) | `unit` STRING, `kind` INT, `pv` REAL, `sp` REAL, `mode` INT, `writable_sp` BOOL, `writable_mode` BOOL, `key` STRING |

Optional for the integration: `name` on climate objects and `device_class` on binary objects (it falls back to the variable name and no device class). Variables must carry the `{PUBLIC_API}` attribute to be visible to TecoApi. Minimal Structured Text:

```
TYPE
  T_HA_Sensor : STRUCT
    name  : STRING[80];
    unit  : STRING[16] := '1';   (* 0 none, 1 °C, 2 %, 3 ppm *)
    kind  : INT := 2;
    value : REAL;
    key   : STRING[64];
  END_STRUCT;
END_TYPE

VAR_GLOBAL
  ha_livingroom_temp_air {PUBLIC_API} : T_HA_Sensor;
END_VAR

(* in the program, every scan: *)
ha_livingroom_temp_air.key   := 'livingroom_temp_air';
ha_livingroom_temp_air.value := livingroom_temperature;
```

A fixture with sample objects lives in `tests/fixtures/ha_contract.json`; `tests/test_contract.py` fails if the integration starts reading a field the types do not define. The reference PLC program (a Mosaic project for a CP-2000, private) keeps the same JSON in sync and checks it against its type definitions in CI. *To confirm: the samples are derived from the PLC source, not captured from a controller.*

*To confirm: how to load a PLC program into the controller (done from TECO's Mosaic tool).*

## Development

```
pip install -r requirements_test.txt
pytest
```

`requirements_test.txt` pulls in `pytest-homeassistant-custom-component`, which needs a recent Python (3.14 at the time of writing). CI runs hassfest, the HACS check and the tests.

## Credits and license

Started as a fork of [hubiktomas/TecoApi](https://github.com/hubiktomas/TecoApi) (itself from jrojko/hass_tecoapi), which has no license; most of the code has since been rewritten (config flow, discovery of the `ha_` objects, climate/sensor/switch/binary platforms, start-up handling, tests). The MIT license in this repository covers the changes made here; if you are the upstream author and want this handled differently, please open an issue.

MIT, see [LICENSE](LICENSE).
