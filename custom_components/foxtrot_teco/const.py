from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import Platform, UnitOfTemperature

DOMAIN = "foxtrot_teco"

CONF_RESOURCE = "resource"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_AUTH_TYPE = "auth_type"
CONF_VERIFY_SSL = "verify_ssl"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_VERIFY_SSL = False
AUTH_NONE = "none"
AUTH_BASIC = "basic"
AUTH_DIGEST = "digest"   # default
AUTH_AUTO = "auto"       # try digest then basic
DEFAULT_SCAN_INTERVAL = 5  # seconds

# Discovery: match nested faces like "<prefix>.HA.<leaf>"
HA_NESTED_PATTERN = r"^(?P<prefix>.+)\.HA\.(?P<leaf>\w+)$"

# Minimal field set a face may expose
FACE_FIELDS_POLL = ("avail", "name", "unit", "pv", "sp", "mode", "kind", "writable_sp", "writable_mode")

# kind values (optional; we also infer)
KIND_NONE = 0
KIND_CLIMATE = 1
KIND_SENSOR = 2
KIND_SWITCH = 3

PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.SWITCH, Platform.BINARY_SENSOR]

UNIT2DEVICE_CLASS = {"0": (None, None,0),"1": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS,1), "2": (SensorDeviceClass.HUMIDITY, "%",0), "3": (SensorDeviceClass.CO2, "ppm",0)}

