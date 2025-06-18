"""The VR Basestation integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.const import (
    CONF_MAC,
    CONF_NAME,
    Platform,
)
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_DEVICE_TYPE,
    DOMAIN,
)
from .services import async_setup_services

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

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
                f"configuration system. Each device will be created as a separate integration entry.\n\n"
                f"Once migration is complete, you can remove the basestation entries from "
                f"your configuration.yaml file.\n\n"
                f"Check Settings → Devices & Services for your migrated devices."
            ),
            "title": "VR Basestation: YAML Migration in Progress",
            "notification_id": "basestation_yaml_migration_notice",
        }

        # Create the notification
        hass.services.call("persistent_notification", "create", notification_data)

    _LOGGER.info("VR Basestation integration setup complete. Bluetooth discovery is now active.")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VR Basestation from a config entry."""
    # Ensure domain data exists
    hass.data.setdefault(DOMAIN, {})

    # Store entry data for this device
    hass.data[DOMAIN][entry.entry_id] = entry.data

    # Set up entities for this entry across all platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up services (only needs to be done once, but calling multiple times is safe)
    await async_setup_services(hass)

    _LOGGER.info(
        "Successfully set up VR Basestation device: %s (%s)",
        entry.title,
        entry.data.get(CONF_MAC, "unknown"),
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading VR Basestation config entry: %s", entry.title)

    # Unload entities for this config entry across all platforms
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Remove stored entry data
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _LOGGER.info("Successfully unloaded VR Basestation device: %s", entry.title)
    else:
        _LOGGER.error("Failed to unload VR Basestation config entry: %s", entry.title)

    return unload_ok
