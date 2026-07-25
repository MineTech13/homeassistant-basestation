"""The VR Basestation integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from homeassistant.const import CONF_MAC, CONF_NAME, Platform

from .const import (
    CONF_CONNECTION_TIMEOUT,
    CONF_DEVICE_TYPE,
    CONF_FAST_POLLING_INTERVAL,
    CONF_INFO_SCAN_INTERVAL,
    CONF_PAIR_ID,
    CONF_POWER_STATE_SCAN_INTERVAL,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_FAST_POLLING_INTERVAL,
    DEFAULT_INFO_SCAN_INTERVAL,
    DEFAULT_POWER_STATE_SCAN_INTERVAL,
    DOMAIN,
    MIN_CONNECTION_TIMEOUT,
)
from .coordinator import BasestationCoordinator, BasestationInfoCoordinator
from .device import BasestationDevice, get_basestation_device

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SWITCH, Platform.BUTTON, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VR Basestation from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    mac = entry.data.get(CONF_MAC)
    name = cast("str|None", entry.data.get(CONF_NAME))
    device_type = cast("str", entry.data.get(CONF_DEVICE_TYPE))
    pair_id = entry.data.get(CONF_PAIR_ID)

    # Get scan interval from options or defaults
    scan_interval = entry.options.get(CONF_POWER_STATE_SCAN_INTERVAL, DEFAULT_POWER_STATE_SCAN_INTERVAL)
    info_scan_interval = entry.options.get(CONF_INFO_SCAN_INTERVAL, DEFAULT_INFO_SCAN_INTERVAL)
    fast_scan_interval = entry.options.get(CONF_FAST_POLLING_INTERVAL, DEFAULT_FAST_POLLING_INTERVAL)

    if mac:
        # max() guards against entries created before MIN_CONNECTION_TIMEOUT was raised, which may
        # still have a lower value stored from before the options form re-validates it.
        connection_timeout = max(
            entry.options.get(CONF_CONNECTION_TIMEOUT, DEFAULT_CONNECTION_TIMEOUT), MIN_CONNECTION_TIMEOUT
        )
        device = get_basestation_device(
            hass,
            mac,
            name=name,
            device_type=device_type,
            pair_id=pair_id,
            connection_timeout=connection_timeout,
            info_scan_interval=info_scan_interval,
        )

        # Setup Coordinators
        coordinator = BasestationCoordinator(hass, device, scan_interval, fast_scan_interval)
        info_coordinator = BasestationInfoCoordinator(hass, device, info_scan_interval)

        # Initial refresh
        await coordinator.async_config_entry_first_refresh()

        # Info-Refresh not blocking as background task
        entry.async_create_background_task(
            hass, info_coordinator.async_request_refresh(), name=f"basestation_info_init_{device.mac}"
        )

        # Store device and coordinators
        hass.data[DOMAIN][entry.entry_id] = {
            "device": device,
            "coordinator": coordinator,
            "info_coordinator": info_coordinator,
        }

    # Register update listener
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)
