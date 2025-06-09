"""The VR Basestation integration."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from bleak import BleakScanner
from bleak.exc import BleakError
from homeassistant.const import (
    CONF_MAC,
    CONF_NAME,
    EVENT_HOMEASSISTANT_STARTED,
    Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_DEVICE_TYPE,
    CONF_DISCOVERY_PREFIX,
    CONF_SETUP_METHOD,
    DEVICE_TYPE_V1,
    DEVICE_TYPE_V2,
    DISCOVERY_INTERVAL,
    DOMAIN,
    SETUP_AUTOMATIC,
    V1_NAME_PREFIX,
    V2_NAME_PREFIX,
)
from .services import async_setup_services

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import CALLBACK_TYPE

_LOGGER = logging.getLogger(__name__)

# Define supported platforms
PLATFORMS = [Platform.SWITCH, Platform.BUTTON, Platform.SENSOR]

# Define validation for a single basestation entry in YAML configuration
BASESTATION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MAC): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_DEVICE_TYPE): cv.string,
    },
)

# Define a schema that allows for both config entries and legacy YAML config
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: cv.schema_with_slug_keys(BASESTATION_SCHEMA),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the VR Basestation component."""
    # Initialize the domain data storage
    hass.data.setdefault(DOMAIN, {})

    # Handle legacy YAML configuration if present
    if DOMAIN in config:
        yaml_devices = config[DOMAIN]
        _LOGGER.info(
            "Found %s basestation(s) configured in YAML. "
            "The integration will automatically migrate these to config entries.",
            len(yaml_devices),
        )

        # Log details about found devices for debugging
        for device_key, device_config in yaml_devices.items():
            mac = device_config.get(CONF_MAC, "unknown")
            name = device_config.get(CONF_NAME, device_key)
            _LOGGER.debug("Found YAML device: %s (MAC: %s, Key: %s)", name, mac, device_key)

        # Create a notification to inform the user about the migration process
        notification_data = {
            "message": (
                f"Found {len(yaml_devices)} VR Basestation(s) in your YAML configuration.\n\n"
                f"The integration is automatically migrating these devices to the new UI-based "
                f"configuration system. This may take a few moments.\n\n"
                f"Once migration is complete, you can remove the basestation entries from "
                f"your configuration.yaml file.\n\n"
                f"Check Settings → Devices & Services for your migrated devices."
            ),
            "title": "VR Basestation: YAML Migration in Progress",
            "notification_id": "basestation_yaml_migration_notice",
        }

        # Create the notification
        hass.services.call("persistent_notification", "create", notification_data)

        # Note: The actual migration will be handled by the switch platform's setup_platform function
        # when Home Assistant processes the switch configuration

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VR Basestation from a config entry."""
    # Ensure domain data exists
    hass.data.setdefault(DOMAIN, {})

    # Initialize automatic discovery if configured
    if entry.data.get(CONF_SETUP_METHOD) == SETUP_AUTOMATIC:
        discovery = BasestationDiscovery(hass, entry)
        await discovery.async_start()
        hass.data[DOMAIN][entry.entry_id] = discovery
    else:
        # Store entry data for regular (manual/selection/import) setups
        hass.data[DOMAIN][entry.entry_id] = entry.data

    # Set up entities for this entry across all platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up services (only needs to be done once, but calling multiple times is safe)
    await async_setup_services(hass)

    _LOGGER.info(
        "Successfully set up VR Basestation config entry: %s (Method: %s)",
        entry.title,
        entry.data.get(CONF_SETUP_METHOD, "unknown"),
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading VR Basestation config entry: %s", entry.title)

    # Unload entities for this config entry across all platforms
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Clean up discovery instance if automatic setup
        if entry.data.get(CONF_SETUP_METHOD) == SETUP_AUTOMATIC:
            discovery = hass.data[DOMAIN].pop(entry.entry_id, None)
            if discovery:
                await discovery.async_stop()
                _LOGGER.debug("Stopped automatic discovery for entry: %s", entry.entry_id)
        else:
            # Remove stored entry data
            hass.data[DOMAIN].pop(entry.entry_id, None)

        _LOGGER.info("Successfully unloaded VR Basestation config entry: %s", entry.title)
    else:
        _LOGGER.error("Failed to unload VR Basestation config entry: %s", entry.title)

    return unload_ok


class BasestationDiscovery:
    """Class to manage automatic discovery of basestations."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the discovery service."""
        self.hass = hass
        self.entry = entry
        # Track devices we've already discovered to avoid duplicates
        self._known_devices = {device[CONF_MAC] for device in entry.data.get("devices", [])}
        # Get the discovery prefix (could be empty for default behavior)
        self._prefix = entry.data.get(CONF_DISCOVERY_PREFIX, "")
        # Callback remover for the interval tracker
        self._remove_interval: CALLBACK_TYPE | None = None
        # Flag to prevent concurrent scans
        self._scanning = False

        _LOGGER.debug(
            "Initialized basestation discovery with prefix '%s', known devices: %s",
            self._prefix,
            len(self._known_devices),
        )

    async def async_start(self) -> None:
        """Start the discovery process."""

        @callback
        async def _async_startup(_event: Any) -> None:
            """Start discovery when Home Assistant is running."""
            _LOGGER.info("Starting automatic basestation discovery")

            # Perform initial scan
            await asyncio.create_task(self._async_scan_devices())

            # Set up periodic scanning
            self._remove_interval = async_track_time_interval(
                self.hass,
                self._async_scan_devices,
                datetime.timedelta(seconds=DISCOVERY_INTERVAL),
            )

            _LOGGER.debug("Automatic discovery started, will scan every %s seconds", DISCOVERY_INTERVAL)

        # Wait for Home Assistant to be fully started before beginning discovery
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _async_startup)

    async def async_stop(self) -> None:
        """Stop the discovery process."""
        if self._remove_interval:
            self._remove_interval()
            self._remove_interval = None
            _LOGGER.debug("Stopped automatic basestation discovery")

    async def _async_scan_devices(self, _: datetime.datetime | None = None) -> None:
        """Scan for new basestation devices."""
        # Prevent concurrent scans which could overwhelm the Bluetooth adapter
        if self._scanning:
            _LOGGER.debug("Scan already in progress, skipping")
            return

        self._scanning = True
        try:
            _LOGGER.debug("Scanning for new basestation devices...")
            devices = await BleakScanner.discover()

            # Filter for new basestation devices that match our criteria
            new_devices = []
            for device in devices:
                # Skip devices without address or name
                if not device.address or not device.name:
                    continue

                # Skip devices we've already discovered
                if device.address in self._known_devices:
                    continue

                # Check if device name matches our criteria
                if self._is_valid_device(device.name):
                    new_devices.append(device)
                    _LOGGER.debug("Found new basestation device: %s (%s)", device.name, device.address)

            if new_devices:
                _LOGGER.info(
                    "Discovered %d new basestation device(s): %s",
                    len(new_devices),
                    ", ".join(f"{device.name} ({device.address})" for device in new_devices),
                )

                # Update the config entry with the new devices
                await self._add_discovered_devices(new_devices)
            else:
                _LOGGER.debug("No new basestation devices found")

        except BleakError as err:
            _LOGGER.warning("BLE error during device discovery: %s", err)
        except Exception as err:
            _LOGGER.exception("Unexpected error during device scanning: %s", err)
        finally:
            self._scanning = False

    async def _add_discovered_devices(self, new_devices) -> None:
        """Add newly discovered devices to the config entry."""
        try:
            # Prepare new device entries
            new_device_entries = []
            for device in new_devices:
                device_entry = {
                    CONF_MAC: device.address,
                    CONF_NAME: device.name,
                    CONF_DEVICE_TYPE: _determine_device_type(device.name),
                }
                new_device_entries.append(device_entry)

            # Update entry data with new devices
            new_data = {
                **self.entry.data,
                "devices": [
                    *self.entry.data.get("devices", []),
                    *new_device_entries,
                ],
            }

            # Update our known devices set
            self._known_devices.update(device.address for device in new_devices)

            # Update the config entry
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)

            # Reload the entry to create new entities for the discovered devices
            _LOGGER.info("Reloading config entry to add %d new devices", len(new_devices))
            await self.hass.config_entries.async_reload(self.entry.entry_id)

        except Exception as err:
            _LOGGER.exception("Error adding discovered devices: %s", err)

    def _is_valid_device(self, name: str) -> bool:
        """Check if device name is valid based on our discovery criteria."""
        # If a specific prefix is configured, only match that prefix
        if self._prefix and name.startswith(self._prefix):
            return True

        # If no specific prefix, check for any known basestation prefix
        if not self._prefix:
            return name.startswith((V1_NAME_PREFIX, V2_NAME_PREFIX))

        return False


def _determine_device_type(name: str | None = None) -> str:
    """Determine device type based on device name."""
    if name and name.startswith(V1_NAME_PREFIX):
        return DEVICE_TYPE_V1

    if name and name.startswith(V2_NAME_PREFIX):
        return DEVICE_TYPE_V2

    # Default to V2 if we can't determine the type
    return DEVICE_TYPE_V2
