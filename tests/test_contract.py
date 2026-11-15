"""The PLC contract: which fields of the ha_* objects the integration reads, and how it maps them.

tests/fixtures/ha_contract.json is a copy of teco/ha_contract.json in the homeautomation
repo (CI there checks it against the PLC source). Keep both identical. The samples in it are
illustrative faces derived from the PLC source, not a capture from the real controller.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.components.climate import ClimateEntityFeature, HVACAction
from homeassistant.components.sensor import SensorDeviceClass

from custom_components.foxtrot_teco.binary_sensor import TecoBinarySensor
from custom_components.foxtrot_teco.climate import TecoClimate
from custom_components.foxtrot_teco.const import UNIT2DEVICE_CLASS
from custom_components.foxtrot_teco.sensor import TecoSensor

CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "ha_contract.json").read_text())
TYPES = {name: set(t["fields"]) for name, t in CONTRACT["types"].items()}
SAMPLES = CONTRACT["samples"]
COMPONENT = Path(__file__).parents[1] / "custom_components" / "foxtrot_teco"

# platform file -> PLC types it consumes
PLATFORM_TYPES = {
    "sensor.py": ["T_HA_Sensor"],
    "binary_sensor.py": ["T_HA_Binary"],
    "switch.py": ["T_HA_Binary"],
    "climate.py": ["T_HA_Climate"],
}
INTERNAL = {"_serial", "_var_path"}          # added by the integration, not by the PLC
# Read, but the integration copes when the PLC does not provide them (falls back to a default).
OPTIONAL = {"climate.py": {"name"}, "binary_sensor.py": {"device_class"}}

READ_RE = re.compile(r"""(?:\bface|\bf|_face\(\))\.get\(\s*["'](\w+)["']|_face\(\)\[\s*["'](\w+)["']\s*\]""")


def coordinator_for(**faces):
    for face in faces.values():
        face["_serial"] = "M5 0075"
    return SimpleNamespace(data=faces, last_update_success=True)


@pytest.mark.parametrize("platform", sorted(PLATFORM_TYPES))
def test_platform_reads_only_fields_the_plc_defines(platform):
    source = (COMPONENT / platform).read_text()
    read = {a or b for a, b in READ_RE.findall(source)}
    allowed = set().union(*(TYPES[t] for t in PLATFORM_TYPES[platform])) | INTERNAL | OPTIONAL.get(platform, set())
    assert not read - allowed, f"{platform} reads {sorted(read - allowed)} which the PLC types do not define"


@pytest.mark.parametrize("platform", sorted(PLATFORM_TYPES))
def test_optional_fields_are_really_absent_from_the_plc_types(platform):
    # keeps OPTIONAL honest: when the PLC gains the field, move it out of OPTIONAL
    defined = set().union(*(TYPES[t] for t in PLATFORM_TYPES[platform]))
    assert not OPTIONAL.get(platform, set()) & defined


def test_samples_follow_their_types():
    by_kind = {t["kind"]: name for name, t in CONTRACT["types"].items()}
    for var, face in SAMPLES.items():
        type_name = by_kind[face["kind"]]
        assert set(face) <= TYPES[type_name], f"{var} has fields outside {type_name}"
        assert set(face) == TYPES[type_name], f"{var} lacks fields of {type_name}"


def test_sensor_unit_codes_used_by_the_plc_are_known():
    for var, face in SAMPLES.items():
        if face["kind"] == 2:
            assert face["unit"] in UNIT2DEVICE_CLASS, f"{var}: unit code {face['unit']!r} unknown"


@pytest.mark.parametrize(
    ("var", "device_class", "unit"),
    [
        ("ha_livingroom_temp_air", SensorDeviceClass.TEMPERATURE, "°C"),
        ("ha_livingroom_humidity", SensorDeviceClass.HUMIDITY, "%"),
        ("ha_livingroom_co2", SensorDeviceClass.CO2, "ppm"),
    ],
)
def test_sensor_face_becomes_a_typed_sensor(var, device_class, unit):
    coordinator = coordinator_for(**{var: dict(SAMPLES[var])})
    entity = TecoSensor(coordinator, var)
    assert entity.device_class == device_class
    assert entity.native_unit_of_measurement == unit
    assert entity.native_value == SAMPLES[var]["value"]
    assert entity.name == var                      # the PLC leaves `name` empty: the variable name is used


def test_read_only_binary_face_becomes_a_binary_sensor():
    var = "ha_livingroom_valves"
    entity = TecoBinarySensor(coordinator_for(**{var: dict(SAMPLES[var])}), client=None, var_path=var)
    assert entity.is_on is True


@pytest.mark.parametrize(("mode", "action"), [(0, HVACAction.IDLE), (1, HVACAction.HEATING), (2, HVACAction.COOLING)])
def test_climate_mode_is_read_as_the_current_action(mode, action):
    # The PLC writes `mode` = cool*2 + heat (fbThermostat), i.e. the current action, although the
    # type comment calls it a setting (0 off, 1 heat, 2 cool, 3 auto). The integration follows
    # the program, not the comment.
    var = "ha_livingroom_climate"
    face = dict(SAMPLES[var], mode=mode)
    entity = TecoClimate(coordinator_for(**{var: face}), client=None, var_path=var)
    assert entity.hvac_action == action
    assert entity.current_temperature == face["pv"]
    assert entity.target_temperature == face["sp"]
    assert ClimateEntityFeature.TARGET_TEMPERATURE in entity.supported_features


def test_climate_without_writable_setpoint_is_read_only():
    var = "ha_livingroom_climate"
    face = dict(SAMPLES[var], writable_sp=False)
    entity = TecoClimate(coordinator_for(**{var: face}), client=None, var_path=var)
    assert ClimateEntityFeature.TARGET_TEMPERATURE not in entity.supported_features
