"""The VR Basestation integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import CONF_MAC, CONF_NAME, Platform
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_DEVICE_TYPE,
    CONF_POWER_STATE_SCAN_INTERVAL,
    DEFAULT_POWER_STATE_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import BasestationCoordinator
from .device import BasestationDevice, get_basestation_device
from .services import async_setup_services

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SWITCH, Platform.BUTTON, Platform.SENSOR]

BASESTATION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MAC): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_DEVICE_TYPE): cv.string,
    },
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: cv.schema_with_slug_keys(BASESTATION_SCHEMA),
    },
    extra=vol.ALLOW_EXTRA,
)

# Constants for MAC formatting
MAC_LENGTH_NO_SEPARATORS = 12
MAC_SEPARATOR_INTERVAL = 2


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the VR Basestation component."""
    hass.data.setdefault(DOMAIN, {})

    if DOMAIN in config:
        yaml_devices = config[DOMAIN]
        _LOGGER.info(
            "Found %s basestation(s) configured in YAML. Starting migration.",
            len(yaml_devices),
        )

        migrated_count = 0
        for device_key, device_config in yaml_devices.items():
            mac = device_config.get(CONF_MAC)
            name = device_config.get(CONF_NAME, device_key)

            if not mac:
                continue

            # Format MAC address
            formatted_mac = mac.replace(":", "").replace("-", "").replace(" ", "").upper()
            if len(formatted_mac) == MAC_LENGTH_NO_SEPARATORS:
                formatted_mac = ":".join(
                    formatted_mac[i : i + MAC_SEPARATOR_INTERVAL]
                    for i in range(0, MAC_LENGTH_NO_SEPARATORS, MAC_SEPARATOR_INTERVAL)
                )

            # Trigger import flow
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": SOURCE_IMPORT}, data={CONF_MAC: formatted_mac, CONF_NAME: name}
                )
            )
            migrated_count += 1

        if migrated_count > 0:
            notification_data = {
                "message": (
                    f"Migrating {migrated_count} VR Basestation(s) from YAML to UI.\n\n"
                    f"You can remove the basestation entries from your configuration.yaml once complete."
                ),
                "title": "VR Basestation: YAML Migration",
                "notification_id": "basestation_yaml_migration_notice",
            }
            hass.services.call("persistent_notification", "create", notification_data)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VR Basestation from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    mac = entry.data.get(CONF_MAC)
    name = entry.data.get(CONF_NAME)
    device_type = entry.data.get(CONF_DEVICE_TYPE)
    pair_id = entry.data.get("pair_id")

    # Get scan interval from options or defaults
    scan_interval = entry.options.get(CONF_POWER_STATE_SCAN_INTERVAL, DEFAULT_POWER_STATE_SCAN_INTERVAL)

    if mac:
        device = get_basestation_device(
            hass,
            mac,
            name=name,
            device_type=device_type,
            pair_id=pair_id,
        )

        # Setup Coordinator
        coordinator = BasestationCoordinator(hass, device, scan_interval)

        # Initial refresh
        await coordinator.async_config_entry_first_refresh()

        # Store device and coordinator
        hass.data[DOMAIN][entry.entry_id] = {"device": device, "coordinator": coordinator}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_setup_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Get device for cleanup
    data = hass.data[DOMAIN].get(entry.entry_id)
    device = data.get("device") if data else None

    if device and isinstance(device, BasestationDevice):
        try:
            await device.cleanup()
        except Exception as e:
            _LOGGER.warning("Error cleaning up device resources: %s", e)

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
