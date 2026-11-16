# Foxtrot TECO for Home Assistant

A Home Assistant integration for **TECO Foxtrot** PLCs (tested with a CP-2000). It talks to the PLC's **PLCComS** communication server and turns the PLC variables that the PLC program publishes under an `ha_` name into Home Assistant entities. The PLC **pushes** every change, so values arrive as they happen and nothing is polled.

It is the Home Assistant half of a two-part interface: **without a PLC program that publishes `ha_*` objects it has nothing to show.** All control logic (heating, valves, schedules) stays in the PLC; Home Assistant mirrors values and can send a few writes back.

## Requirements

- A Foxtrot PLC whose **PLCComS** server is reachable from Home Assistant (TCP, port 5010 by default). On the CP-2000 it runs on the controller itself; `GETINFO:` answers with its version. *To confirm: whether every Foxtrot generation has it built in, or needs the PLCComS program on a separate machine.*
- A PLC program that publishes objects of the three types below, with names starting `ha_`, **in the public file PLCComS serves**. `GETINFO:PUBFILE` (for example `telnet <plc> 5010`, then `GETINFO:`) shows which files that is. On the author's controller it is `iFoxtrot.pub`, which Mosaic generates from the variables marked `{PUBLIC_IFOX}`; the `ha_*` variables carry that attribute.
- Home Assistant 2024.12 or newer.

PLCComS has **no authentication**: keep port 5010 on your LAN.

## Installation

**HACS (recommended):** HACS, three-dot menu, *Custom repositories*, add `https://github.com/jeanpijon/homeassistant-foxtrot` with category *Integration*, download, restart Home Assistant.

**Manual:** copy `custom_components/foxtrot_teco` into your Home Assistant `config/custom_components/` and restart.

## Setup

*Settings, Devices & services, Add integration, "Foxtrot TECO"*.

| Field | Meaning |
|---|---|
| Host/IP of the PLC | e.g. `192.168.11.11` |
| PLCComS port | `5010` unless you changed it |

Setup connects, reads the PLC version for the entry title, and refuses a PLC that lists no `ha_*` variables (the message says to publish them in the public file). No login is stored.

A new installation is identified by the PLC's address; entities are named after it (`sensor.foxtrot_192_168_11_11_ha_livingroom_temp_air`).

## What becomes an entity

On setup (and again on reload) the integration lists the PLC variables whose names start with `ha_` and reads their values. The `kind` field of the object decides what it is:

| `kind` | PLC type | Home Assistant entity |
|---|---|---|
| 1 | `T_HA_Climate` | `climate`: current temperature `pv`, target `sp`, action from `mode`; with `prog` set, also presets and a setpoint the PLC keeps (see Thermostats) |
| 2 | `T_HA_Sensor` | `sensor`: `value`, with device class and unit from the `unit` code |
| 3, `writable` false | `T_HA_Binary` | `binary_sensor`: `state` |
| 3, `writable` true | `T_HA_Binary` | `switch`: `state` (on/off writes `state`) |

Sensor `unit` codes: `0` none, `1` °C (temperature), `2` % (humidity), `3` ppm (CO2).

Entities hang under one device per PLC. The entity name is the object's `name` if the PLC sets one, otherwise the variable name. The unique id is a hash of the device identity, kind and the object's `key` (or its variable name if `key` is empty): **keep `key` stable in the PLC program, changing it creates a new entity.**

## Thermostats (presets and setpoint)

If a climate object's `prog` field holds the name of a thermostat's time program (iControlLib `fb_iTimeProgWeek`, the block the iFoxtrot app shows as a thermostat), the integration finds that program's `GTSAP1_TPW_*` variables and the climate entity works like the iFoxtrot thermostat:

- **Presets:** `schedule` (follow the weekly program, *auto*), `comfort` (day), `sleep` (night), `eco` and `limit`. Choosing one of the last four makes the room manual in that mode. **A manual setting ends by itself at the next switch time of the weekly program**, as in the app. `hvac_mode` *auto* is the same as `schedule`.
- **Setpoint:** moves in 0.5 K steps, at most 5 K either way from the current mode's program temperature (that is the range Home Assistant offers). Each step is a command the PLC confirms before the next goes out, so a large change takes a few seconds. Setting a temperature while the room is in *schedule* makes it manual, as in the app.
- **Attributes:** `holiday` (the program's holiday period is on) and `delta` (the offset in K).
- The weekly program itself is not edited here yet.

Without `prog`, or when the program is not published by the PLC (or two have the same name), the climate entity falls back to writing `sp`, which the PLC program may overwrite.

## How it behaves

- **Push.** At start-up the integration lists the `ha_*` variables, asks PLCComS to push changes of all of them and reads their values once. From then on a change arrives as it happens (a REAL only when it moved by more than 0.1, which is below what the entities display). Changes that arrive together become one state update.
- **Alive checks.** Every 30 s it asks the PLC whether its program runs (`__PLC_RUN`). It also re-reads everything every 5 minutes in case a push was lost.
- **Outages.** If the connection drops, the PLC stops answering or its program stops, the entities become unavailable and the integration reconnects by itself (5 s, doubling up to 60 s). On return it reads everything again, so changes made meanwhile show up.
- **Late PLC at start.** If the PLC is not up when Home Assistant starts (no connection, no `ha_*` variables yet, program not running), setup fails and Home Assistant retries by itself, with back-off up to 10 minutes. It never loads a half-empty entity set.
- **New PLC program.** When a new program is loaded into the PLC, PLCComS says so and the integration re-reads everything. If the set of `ha_*` objects changed, the integration reloads itself, so new objects appear without a manual reload.
- **Writes** (`climate` setpoint, `switch`): sent to the PLC and confirmed by the PLC's own push. If the PLC does not take the value, the action fails with an error.

## Limitations

- Only objects named `ha_*` are visible. Anything else in the PLC is out of scope.
- `climate`: the only HVAC mode is *auto* (there is no off). `mode` from the PLC is read as the **current action** (0 idle, 1 heating, 2 cooling), which is what the reference PLC program writes, although the PLC type comment describes it as a setting.
- `climate` without a time program: Home Assistant writes `sp`; whether the value sticks depends on the PLC program (a thermostat block that assigns `sp` from its own schedule overwrites it on every scan).
- One PLC per config entry; no YAML configuration.
- PLCComS answers about four requests a second, so the integration keeps its conversation to a minimum (one read at start, then pushes).

## Upgrading from 1.x (TecoApi)

1.x polled the PLC's TecoApi web interface every 5 seconds. 2.0 replaces that completely; **TecoApi and its user account are no longer used** by this integration. On the first start 2.0 converts the existing entry by itself: the host is kept, the port becomes 5010, the login is dropped, and the PLC identity is taken over from the device 1.x registered, so entities, entity ids and history stay as they are. Before upgrading, make sure the PLC publishes the `ha_*` variables in the public file PLCComS serves (see Requirements); otherwise the entry waits (and retries) until it does.

Going back to 1.x needs the Home Assistant `.storage` files from before the upgrade (the converted entry cannot be read by 1.x).

## The PLC side (the contract)

The integration reads exactly these fields; the names and types come from the PLC program's type definitions:

| Type | Fields (PLC type) |
|---|---|
| `T_HA_Sensor` (`kind` 2) | `name` STRING, `unit` STRING, `kind` INT, `value` REAL, `key` STRING |
| `T_HA_Binary` (`kind` 3) | `name` STRING, `kind` INT, `state` BOOL, `writable` BOOL, `key` STRING |
| `T_HA_Climate` (`kind` 1) | `unit` STRING, `kind` INT, `pv` REAL, `sp` REAL, `mode` INT, `writable_sp` BOOL, `writable_mode` BOOL, `key` STRING, `prog` STRING |

Optional for the integration: `name` on climate objects, `device_class` on binary objects (it falls back to the variable name and no device class) and `prog` (without it a climate object has no presets). The time-program variables (`GTSAP1_TPW_*`) must be published in the same public file; iControlLib marks them `{PUBLIC_IFOX}` already. Variables must be published in the public file PLCComS serves, as described under Requirements. Minimal Structured Text:

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
  ha_livingroom_temp_air {PUBLIC_IFOX} : T_HA_Sensor;      (* or the attribute of the public file PLCComS serves *)
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

Started as a fork of [hubiktomas/TecoApi](https://github.com/hubiktomas/TecoApi) (itself from jrojko/hass_tecoapi), which has no license; most of the code has since been rewritten (config flow, discovery of the `ha_` objects, climate/sensor/switch/binary platforms, start-up handling, tests), and since 2.0 the TecoApi client of the original is gone altogether, replaced by a PLCComS client. The MIT license in this repository covers the changes made here; if you are the upstream author and want this handled differently, please open an issue.

MIT, see [LICENSE](LICENSE).
