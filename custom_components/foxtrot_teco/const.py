from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import Platform, UnitOfTemperature

DOMAIN = "foxtrot_teco"

CONF_SERIAL = "serial"

# PLC objects that become entities: structs named "ha_<name>" whose fields are "ha_<name>.<field>".
HA_PREFIX = "ha_"

# The room thermostats' time programs (iControlLib fb_iTimeProgWeek): GTSAP1_TPW_* variables, which
# iFoxtrot also uses. They are found by name and tied to a climate object through its ``prog`` field.
TPW = "gtsap1_tpw_"
TPW_TIMEOUT = 3.0        # s the PLC gets to show the effect of a command
TPW_POLL = 0.05          # s between looks at the values while waiting for it

# A REAL is pushed only when it moved by more than this (the entities show one decimal at most).
REAL_DELTA = 0.1

PING_INTERVAL = 30       # s between checks that the PLC program runs (also proves the link is alive)
RESYNC_INTERVAL = 300    # s between full re-reads, in case a push was lost
RECONNECT_MIN = 5        # s, doubled up to RECONNECT_MAX while the PLC stays away
RECONNECT_MAX = 60
FLUSH_DELAY = 0.1        # s: pushes arriving together become one state update

# kind values (optional; we also infer)
KIND_NONE = 0
KIND_CLIMATE = 1
KIND_SENSOR = 2
KIND_SWITCH = 3

PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.SWITCH, Platform.BINARY_SENSOR]

UNIT2DEVICE_CLASS = {"0": (None, None,0),"1": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS,1), "2": (SensorDeviceClass.HUMIDITY, "%",0), "3": (SensorDeviceClass.CO2, "ppm",0)}
