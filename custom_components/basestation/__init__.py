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

# Options only the 2.0.x forms saved, which nothing reads any more.
OBSOLETE_OPTIONS = ("enable_info_sensors", "standby_scan_interval", "enable_power_state_sensor")

# The 2.0.x options forms pre-filled their own defaults and saved every field on submit, so an entry
# whose options were ever opened is pinned to these rather than following today's defaults (20 s and
# 60 s). The 5 s state poll is 2.0.0's; 2.0.1's form pre-filled the stored value, so it stayed.
PINNED_2_0_DEFAULTS = {CONF_CONNECTION_TIMEOUT: 10, CONF_POWER_STATE_SCAN_INTERVAL: 5}


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry from an older version."""
    if entry.version > 1:
        # Written by a newer major version this one can't read (a downgrade).
        return False

    if entry.minor_version < 2:  # noqa: PLR2004
        data = dict(entry.data)
        options = {key: value for key, value in entry.options.items() if key not in OBSOLETE_OPTIONS}

        # 2.0.x took the device name from options (pre-filled from data), 2.1 reads data only.
        if (name := options.pop(CONF_NAME, None)) and name != data.get(CONF_NAME):
            data[CONF_NAME] = name
            _LOGGER.info("%s: kept the device name %s set in the 2.0 options", entry.title, name)

        reset = {key: options.pop(key) for key, pinned in PINNED_2_0_DEFAULTS.items() if options.get(key) == pinned}
        # 2.0.x also allowed a timeout down to 5 s, which today's form rejects.
        if options.get(CONF_CONNECTION_TIMEOUT, MIN_CONNECTION_TIMEOUT) < MIN_CONNECTION_TIMEOUT:
            reset[CONF_CONNECTION_TIMEOUT] = options.pop(CONF_CONNECTION_TIMEOUT)
        if reset:
            # Can't tell a value saved on purpose from a pinned default, so say what changed.
            _LOGGER.info(
                "%s: reset %s to the current default, since 2.0 saved its own defaults whenever the options "
                "were opened. If you chose a value on purpose, set it again in the options",
                entry.title,
                ", ".join(f"{key} (was {value})" for key, value in reset.items()),
            )

        hass.config_entries.async_update_entry(entry, data=data, options=options, minor_version=2)
        _LOGGER.debug("Migrated config entry %s to version 1.2", entry.title)

    return True


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
        # The migration drops 2.0.x's shorter timeouts, but an entry saved again by 2.0.1 after a
        # downgrade is already at 1.2 and isn't migrated twice.
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
        coordinator = BasestationCoordinator(hass, entry, device, scan_interval, fast_scan_interval)
        info_coordinator = BasestationInfoCoordinator(hass, entry, device, info_scan_interval)

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
