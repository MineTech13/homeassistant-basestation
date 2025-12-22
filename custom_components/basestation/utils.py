"""Utility functions for the VR Basestation integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_MAC, CONF_NAME

from .const import (
    CONF_CONNECTION_TIMEOUT,
    CONF_DEVICE_TYPE,
    CONF_ENABLE_INFO_SENSORS,
    CONF_ENABLE_POWER_STATE_SENSOR,
    CONF_INFO_SCAN_INTERVAL,
    CONF_PAIR_ID,
    CONF_POWER_STATE_SCAN_INTERVAL,
    CONF_STANDBY_SCAN_INTERVAL,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_ENABLE_INFO_SENSORS,
    DEFAULT_ENABLE_POWER_STATE_SENSOR,
    DEFAULT_INFO_SCAN_INTERVAL,
    DEFAULT_POWER_STATE_SCAN_INTERVAL,
    DEFAULT_STANDBY_SCAN_INTERVAL,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


def get_basic_device_config(entry: ConfigEntry) -> dict[str, Any] | None:
    """
    Extract basic device configuration from a config entry.

    This function is used by button and switch platforms that only need
    the core device information without sensor-specific options.

    Args:
        entry: The config entry to extract data from

    Returns:
        Dictionary with basic device configuration, or None if MAC is missing

    """
    # Extract core device data
    mac = entry.data.get(CONF_MAC)
    name = entry.data.get(CONF_NAME)
    device_type = entry.data.get(CONF_DEVICE_TYPE)
    pair_id = entry.data.get(CONF_PAIR_ID)
    setup_method = entry.data.get("setup_method", "unknown")

    # Check for required MAC address
    if not mac:
        _LOGGER.error("No MAC address found in config entry data: %s", entry.data)
        return None

    # Override device name if set in options
    options = entry.options
    if options.get(CONF_NAME):
        name = options[CONF_NAME]

    # Get connection timeout from options (used by all platforms)
    connection_timeout = options.get(CONF_CONNECTION_TIMEOUT, DEFAULT_CONNECTION_TIMEOUT)

    _LOGGER.debug(
        "Extracted basic config - MAC: %s, Name: %s, Type: %s, Method: %s",
        mac,
        name,
        device_type,
        setup_method,
    )

    return {
        "mac": mac,
        "name": name,
        "device_type": device_type,
        "pair_id": pair_id,
        "setup_method": setup_method,
        "connection_timeout": connection_timeout,
    }


def get_sensor_device_config(entry: ConfigEntry) -> dict[str, Any] | None:
    """
    Extract full device configuration including sensor-specific options.

    This function is used by the sensor platform which needs additional
    configuration for scan intervals and sensor enablement.

    Args:
        entry: The config entry to extract data from

    Returns:
        Dictionary with full device configuration including sensor options,
        or None if basic config is invalid

    """
    # Get basic device config first
    basic_config = get_basic_device_config(entry)
    if not basic_config:
        return None

    # Get sensor-specific options
    options = entry.options
    enable_info_sensors = options.get(CONF_ENABLE_INFO_SENSORS, DEFAULT_ENABLE_INFO_SENSORS)
    enable_power_state_sensor = options.get(CONF_ENABLE_POWER_STATE_SENSOR, DEFAULT_ENABLE_POWER_STATE_SENSOR)
    info_scan_interval = options.get(CONF_INFO_SCAN_INTERVAL, DEFAULT_INFO_SCAN_INTERVAL)
    power_state_scan_interval = options.get(CONF_POWER_STATE_SCAN_INTERVAL, DEFAULT_POWER_STATE_SCAN_INTERVAL)
    standby_scan_interval = options.get(CONF_STANDBY_SCAN_INTERVAL, DEFAULT_STANDBY_SCAN_INTERVAL)

    _LOGGER.info(
        "Setting up sensors for device: %s (%s) - Info sensors: %s, Power state sensor: %s",
        basic_config["name"] or basic_config["mac"],
        basic_config["mac"],
        enable_info_sensors,
        enable_power_state_sensor,
    )

    # Combine basic config with sensor-specific options
    return {
        **basic_config,
        "enable_info_sensors": enable_info_sensors,
        "enable_power_state_sensor": enable_power_state_sensor,
        "info_scan_interval": info_scan_interval,
        "power_state_scan_interval": power_state_scan_interval,
        "standby_scan_interval": standby_scan_interval,
    }
