"""Constants for the Valve Index Basestation integration."""

DOMAIN = "basestation"

# GATT Characteristic and values
PWR_CHARACTERISTIC = "00001525-1212-EFDE-1523-785FEABCD124"
PWR_ON = b"\x01"
PWR_STANDBY = b"\x00"

# Device detection
DEFAULT_DEVICE_PREFIX = "LHB-"

# Configuration
CONF_DISCOVERY_PREFIX = "discovery_prefix"
CONF_SETUP_METHOD = "setup_method"

# Setup methods
SETUP_AUTOMATIC = "automatic"
SETUP_SELECTION = "selection"
SETUP_MANUAL = "manual"
SETUP_IMPORT = "import"  # Added for migration from YAML config

# Discovery settings
DISCOVERY_INTERVAL = 60  # seconds

# Connection settings
CONNECTION_RETRY_DELAY = 1  # seconds
CONNECTION_MAX_RETRIES = 3