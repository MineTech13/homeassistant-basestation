"""Constants for the VR Basestation integration."""

DOMAIN = "basestation"

# Device types
DEVICE_TYPE_V1 = "vive"
DEVICE_TYPE_V2 = "valve"

# Valve Index Basestation (V2) constants
V2_PWR_SERVICE = "00001523-1212-efde-1523-785feabcd124"
V2_PWR_CHARACTERISTIC = "00001525-1212-EFDE-1523-785FEABCD124"
V2_CHANNEL_CHARACTERISTIC = "00001524-1212-EFDE-1523-785FEABCD124"
V2_IDENTIFY_CHARACTERISTIC = "00008421-1212-EFDE-1523-785FEABCD124"
V2_PWR_ON = b"\x01"
V2_PWR_STANDBY = b"\x02"
V2_PWR_SLEEP = b"\x00"

# Vive Basestation (V1) constants
V1_PWR_SERVICE = "0000cb00-0000-1000-8000-00805f9b34fb"
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
CONF_CONNECTION_TIMEOUT = "connection_timeout"
CONF_ENABLE_INFO_SENSORS = "enable_info_sensors"

# Deprecated options (kept for migration compatibility only)
# These are no longer used but kept to prevent errors when upgrading
CONF_STANDBY_SCAN_INTERVAL = "standby_scan_interval"  # DEPRECATED in v2.0.3 - standby switch reads from cache
CONF_ENABLE_POWER_STATE_SENSOR = "enable_power_state_sensor"  # DEPRECATED in v2.0.3 - always enabled (critical)

# Setup methods - simplified for device-based architecture
SETUP_MANUAL = "manual"
SETUP_IMPORT = "import"  # For migration from YAML config

# Name prefixes for bluetooth device recognition
V1_NAME_PREFIX = "HTC BS"
V2_NAME_PREFIX = "LHB-"

# Power state descriptions for V2 basestations
V2_STATE_DESCRIPTIONS = {
    0x00: "Sleep",
    0x01: "Starting Up",
    0x02: "Standby",
    0x08: "Booting",
    0x09: "Booting",
    0x0B: "On",
}

# Default scan intervals (in seconds)
DEFAULT_INFO_SCAN_INTERVAL = 1800  # 30 minutes - for static info sensors
DEFAULT_POWER_STATE_SCAN_INTERVAL = 5  # 5 seconds - for power state sensor (controls ALL state freshness)
DEFAULT_CONNECTION_TIMEOUT = 10  # 10 seconds - BLE connection timeout

# Default sensor enablement
DEFAULT_ENABLE_INFO_SENSORS = True  # Enable device info sensors by default

# Legacy constants for backward compatibility
INFO_SENSOR_SCAN_INTERVAL = DEFAULT_INFO_SCAN_INTERVAL
POWER_STATE_SCAN_INTERVAL = DEFAULT_POWER_STATE_SCAN_INTERVAL
# STANDBY_SWITCH_SCAN_INTERVAL removed - no longer used in v2.0.3

# Initial device info setup retries
INITIAL_RETRY_DELAY = 2  # seconds
MAX_INITIAL_RETRIES = 3  # number of retries

# Number of failures allowed before operation is considered unsuccessful
MAX_CONSECUTIVE_FAILURES = 3

# Magic number constants to avoid PLR2004 violations
STANDBY_STATE_VALUE = 0x02  # State value for standby mode
