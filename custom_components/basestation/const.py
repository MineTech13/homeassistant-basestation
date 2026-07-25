"""Constants for the VR Basestation integration."""

from enum import Enum, IntEnum

DOMAIN = "basestation"

# Device types
DEVICE_TYPE_V1 = "vive"
DEVICE_TYPE_V2 = "valve"

# Valve Index Basestation (V2) constants
V2_PWR_CHARACTERISTIC = "00001525-1212-EFDE-1523-785FEABCD124"
V2_CHANNEL_CHARACTERISTIC = "00001524-1212-EFDE-1523-785FEABCD124"
V2_IDENTIFY_CHARACTERISTIC = "00008421-1212-EFDE-1523-785FEABCD124"

# Vive Basestation (V1) constants
V1_PWR_CHARACTERISTIC = "0000cb01-0000-1000-8000-00805f9b34fb"

# Standard BLE characteristics for device information
FIRMWARE_CHARACTERISTIC = "00002A26-0000-1000-8000-00805F9B34FB"
MODEL_CHARACTERISTIC = "00002A24-0000-1000-8000-00805F9B34FB"
HARDWARE_CHARACTERISTIC = "00002A27-0000-1000-8000-00805F9B34FB"
MANUFACTURER_CHARACTERISTIC = "00002A29-0000-1000-8000-00805F9B34FB"

# Configuration options
CONF_DEVICE_TYPE = "device_type"
CONF_PAIR_ID = "pair_id"  # For V1 basestations
CONF_SETUP_METHOD = "setup_method"

# Options flow configuration keys (current)
CONF_INFO_SCAN_INTERVAL = "info_scan_interval"
CONF_POWER_STATE_SCAN_INTERVAL = "power_state_scan_interval"
CONF_FAST_POLLING_INTERVAL = "fast_polling_interval"
CONF_CONNECTION_TIMEOUT = "connection_timeout"

# Setup methods - simplified for device-based architecture
SETUP_MANUAL = "manual"

# Name prefixes for bluetooth device recognition
V1_NAME_PREFIX = "HTC BS"
V2_NAME_PREFIX = "LHB-"


class BasestationPowerState(IntEnum):
    """Power states for Valve Basestations."""

    SLEEP = 0x00
    STARTING_UP = 0x01
    STANDBY = 0x02
    BOOTING_1 = 0x08
    BOOTING_2 = 0x09
    ON = 0x0B


class V1Command(bytes, Enum):
    """Command prefixes for Vive Basestations (V1)."""

    TURN_ON = b"\x12\x00\x00\x00"
    TURN_OFF = b"\x12\x02\x00\x01"


class V2Command(bytes, Enum):
    """Specific commands for Valve Basestations (V2)."""

    IDENTIFY = b"\x00"


# Power state descriptions for V2 basestations
V2_STATE_DESCRIPTIONS: dict[int, str] = {
    BasestationPowerState.SLEEP: "Sleep",
    BasestationPowerState.STARTING_UP: "Starting Up",
    BasestationPowerState.STANDBY: "Standby",
    BasestationPowerState.BOOTING_1: "Booting",
    BasestationPowerState.BOOTING_2: "Booting",
    BasestationPowerState.ON: "On",
}

# Default scan intervals (in seconds)
DEFAULT_INFO_SCAN_INTERVAL = 1800  # 30 minutes - for static info sensors
DEFAULT_POWER_STATE_SCAN_INTERVAL = 60  # 60 seconds - for power state sensor (controls ALL state freshness)
DEFAULT_FAST_POLLING_INTERVAL = 5  # 5 seconds - for fast polling during boot

# bleak-retry-connector (which establishes every BLE connection this integration makes) uses its
# own internal ~20s timeout and only fails through its own backoff/cleanup handling once that
# fires. A shorter connection_timeout here would mean we cancel the connection attempt ourselves
# first instead - which a remote BLE proxy may not see as a clean disconnect, leaving it holding a
# stale connection slot. 20s is therefore a hard floor, not just a suggested minimum.
MIN_CONNECTION_TIMEOUT = 20
DEFAULT_CONNECTION_TIMEOUT = 30  # some headroom above MIN_CONNECTION_TIMEOUT
