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

# Define validation for a single basestation entry
BASESTATION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MAC): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_DEVICE_TYPE): cv.string,
    },
)

# Define a schema that allows for both config entries and legacy config
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: cv.schema_with_slug_keys(BASESTATION_SCHEMA),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the VR Basestation component."""
    hass.data.setdefault(DOMAIN, {})

    # Check for legacy configuration (YAML) and set up migration
    if DOMAIN in config:
        _LOGGER.info(
            "Found %s basestation(s) configured in YAML. These will be imported to the new configuration system.",
            len(config[DOMAIN]),
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VR Basestation from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Initialize automatic discovery if configured
    if entry.data.get(CONF_SETUP_METHOD) == SETUP_AUTOMATIC:
        discovery = BasestationDiscovery(hass, entry)
        await discovery.async_start()
        hass.data[DOMAIN][entry.entry_id] = discovery
    else:
        hass.data[DOMAIN][entry.entry_id] = entry.data

    # Set up entities for this entry
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up services
    await async_setup_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload entities for this config entry
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Clean up discovery instance if automatic setup
        if entry.data.get(CONF_SETUP_METHOD) == SETUP_AUTOMATIC:
            discovery = hass.data[DOMAIN].pop(entry.entry_id)
            await discovery.async_stop()
        else:
            hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


class BasestationDiscovery:
    """Class to manage automatic discovery of basestations."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the discovery service."""
        self.hass = hass
        self.entry = entry
        self._known_devices = {device[CONF_MAC] for device in entry.data.get("devices", [])}
        self._prefix = entry.data.get(CONF_DISCOVERY_PREFIX, "")
        self._remove_interval: CALLBACK_TYPE | None = None
        self._scanning = False

    async def async_start(self) -> None:
        """Start the discovery process."""

        @callback
        async def _async_startup(_event: Any) -> None:
            """Start discovery when Home Assistant is running."""
            await asyncio.create_task(self._async_scan_devices())
            self._remove_interval = async_track_time_interval(
                self.hass,
                self._async_scan_devices,
                datetime.timedelta(seconds=DISCOVERY_INTERVAL),
            )

        # Start discovery task on bootup
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _async_startup)

    async def async_stop(self) -> None:
        """Stop the discovery process."""
        if self._remove_interval:
            self._remove_interval()
            self._remove_interval = None

    async def _async_scan_devices(self, _: datetime.datetime | None = None) -> None:
        """Scan for new devices."""
        # Prevent concurrent scans
        if self._scanning:
            return

        self._scanning = True
        try:
            _LOGGER.debug("Scanning for new basestation devices...")
            devices = await BleakScanner.discover()

            # Filter for new basestation devices
            new_devices = []
            for device in devices:
                if not device.address or not device.name or device.address in self._known_devices:
                    continue

                # Check if device name matches any of our prefixes
                if self._is_valid_device(device.name):
                    new_devices.append(device)

            if new_devices:
                _LOGGER.info(
                    "Discovered %d new basestation device(s): %s",
                    len(new_devices),
                    ", ".join(f"{device.name} ({device.address})" for device in new_devices),
                )

                # Update known devices
                new_data = {
                    **self.entry.data,
                    "devices": [
                        *self.entry.data.get("devices", []),
                        *[
                            {
                                CONF_MAC: device.address,
                                CONF_NAME: device.name,
                                CONF_DEVICE_TYPE: _determine_device_type(device.name),
                            }
                            for device in new_devices
                        ],
                    ],
                }

                self._known_devices.update(device.address for device in new_devices)

                # Update entry data
                self.hass.config_entries.async_update_entry(self.entry, data=new_data)

                # Reload entry to create new entities
                await self.hass.config_entries.async_reload(self.entry.entry_id)
            else:
                _LOGGER.debug("No new basestation devices found")

        except BleakError as err:
            _LOGGER.warning("BLE error during device discovery: %s", err)
        except Exception:
            _LOGGER.exception("Error scanning for devices")
        finally:
            self._scanning = False

    def _is_valid_device(self, name: str) -> bool:
        """Check if device name is valid based on prefix."""
        if self._prefix and name.startswith(self._prefix):
            return True

        # If no specific prefix, check for any known basestation prefix
        if not self._prefix:
            return name.startswith((V1_NAME_PREFIX, V2_NAME_PREFIX))

        return False


def _determine_device_type(name: str | None = None) -> str:
    """Determine device type based on name."""
    if name and name.startswith(V1_NAME_PREFIX):
        return DEVICE_TYPE_V1

    if name and name.startswith(V2_NAME_PREFIX):
        return DEVICE_TYPE_V2

    return DEVICE_TYPE_V2  # Default to V2 if we can't determine
