"""Sensor component for basestation integration."""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, CONF_NAME, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_CONNECTION_TIMEOUT,
    CONF_DEVICE_TYPE,
    CONF_ENABLE_INFO_SENSORS,
    CONF_ENABLE_POWER_STATE_SENSOR,
    CONF_INFO_SCAN_INTERVAL,
    CONF_PAIR_ID,
    CONF_POWER_STATE_SCAN_INTERVAL,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_ENABLE_INFO_SENSORS,
    DEFAULT_ENABLE_POWER_STATE_SENSOR,
    DEFAULT_INFO_SCAN_INTERVAL,
    DEFAULT_POWER_STATE_SCAN_INTERVAL,
    DOMAIN,
    INITIAL_RETRY_DELAY,
    MAX_CONSECUTIVE_FAILURES,
    MAX_INITIAL_RETRIES,
    V2_STATE_DESCRIPTIONS,
)
from .device import (
    BasestationDevice,
    ValveBasestationDevice,
    ViveBasestationDevice,
    get_basestation_device,
)

if TYPE_CHECKING:
    from .device import BaseStationDeviceInfoKey


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the basestation sensors."""
    _LOGGER.debug("Setting up basestation sensor entities for entry: %s", entry.title)

    # Get device configuration from the config entry
    device_config = _extract_device_config(entry)
    if not device_config:
        return

    # Create the basestation device instance with user-configured timeout
    device = get_basestation_device(
        hass,
        device_config["mac"],
        name=device_config["name"],
        device_type=device_config["device_type"],
        pair_id=device_config["pair_id"],
        connection_timeout=device_config["connection_timeout"],
    )

    if not device:
        _LOGGER.error("Failed to create device for MAC: %s", device_config["mac"])
        return

    # Try initial device info read with retries (only if info sensors are enabled)
    if device_config["enable_info_sensors"]:
        await _perform_initial_device_info_read(device, device_config["mac"])

    # Create sensor entities based on user preferences
    entities = _create_sensor_entities(device, device_config)

    # Add all sensor entities to Home Assistant
    if entities:
        async_add_entities(entities, update_before_add=True)
        _LOGGER.info(
            "Successfully added %d sensor entities for %s setup: %s",
            len(entities),
            device_config["setup_method"],
            device_config["name"] or device_config["mac"],
        )
    else:
        _LOGGER.info(
            "No sensor entities enabled for %s setup: %s",
            device_config["setup_method"],
            device_config["name"] or device_config["mac"],
        )


def _extract_device_config(entry: ConfigEntry) -> dict[str, Any] | None:
    """Extract and validate device configuration from config entry."""
    mac = entry.data.get(CONF_MAC)
    name = entry.data.get(CONF_NAME)
    device_type = entry.data.get(CONF_DEVICE_TYPE)
    pair_id = entry.data.get(CONF_PAIR_ID)
    setup_method = entry.data.get("setup_method", "unknown")

    # Get user options or use defaults
    options = entry.options
    enable_info_sensors = options.get(CONF_ENABLE_INFO_SENSORS, DEFAULT_ENABLE_INFO_SENSORS)
    enable_power_state_sensor = options.get(CONF_ENABLE_POWER_STATE_SENSOR, DEFAULT_ENABLE_POWER_STATE_SENSOR)
    info_scan_interval = options.get(CONF_INFO_SCAN_INTERVAL, DEFAULT_INFO_SCAN_INTERVAL)
    power_state_scan_interval = options.get(CONF_POWER_STATE_SCAN_INTERVAL, DEFAULT_POWER_STATE_SCAN_INTERVAL)
    connection_timeout = options.get(CONF_CONNECTION_TIMEOUT, DEFAULT_CONNECTION_TIMEOUT)

    # Override device name if set in options
    if options.get(CONF_NAME):
        name = options[CONF_NAME]

    if not mac:
        _LOGGER.error("No MAC address found in config entry data: %s", entry.data)
        return None

    _LOGGER.info("Setting up sensors for device: %s (%s)", name or mac, mac)

    return {
        "mac": mac,
        "name": name,
        "device_type": device_type,
        "pair_id": pair_id,
        "setup_method": setup_method,
        "enable_info_sensors": enable_info_sensors,
        "enable_power_state_sensor": enable_power_state_sensor,
        "info_scan_interval": info_scan_interval,
        "power_state_scan_interval": power_state_scan_interval,
        "connection_timeout": connection_timeout,
    }


async def _perform_initial_device_info_read(device: BasestationDevice, mac: str) -> None:
    """Perform initial device info read with retries."""
    for retry in range(MAX_INITIAL_RETRIES):
        try:
            if retry > 0:
                _LOGGER.debug(
                    "Retry %d/%d for initial device info read for %s",
                    retry + 1,
                    MAX_INITIAL_RETRIES,
                    mac,
                )
                await asyncio.sleep(INITIAL_RETRY_DELAY * (retry + 1))

            # Force initial read to ensure we get the data
            device_info = await device.read_device_info(force=True)
            if device_info:
                _LOGGER.info(
                    "Initial device info read successful for %s: %s",
                    mac,
                    ", ".join(device_info.keys()),
                )
                break
        except Exception as e:
            _LOGGER.warning(
                "Error during initial device info read for %s (retry %d/%d): %s",
                mac,
                retry + 1,
                MAX_INITIAL_RETRIES,
                e,
            )


def _create_sensor_entities(device: BasestationDevice, config: dict[str, Any]) -> list[SensorEntity]:
    """Create sensor entities based on device type and user preferences."""
    entities = []

    # Add info sensors only if enabled
    if config["enable_info_sensors"]:
        entities.extend(_create_info_sensors(device, config["info_scan_interval"]))

    # Add device-specific sensors
    if isinstance(device, ValveBasestationDevice):
        entities.extend(_create_valve_sensors(device, config))
    elif isinstance(device, ViveBasestationDevice):
        entities.extend(_create_vive_sensors(device, config))

    return entities


def _create_info_sensors(device: BasestationDevice, scan_interval: int) -> list[SensorEntity]:
    """Create common device info sensors."""
    return [
        BasestationInfoSensor(device, "firmware", "Firmware", "mdi:developer-board", scan_interval),
        BasestationInfoSensor(device, "model", "Model", "mdi:card-text", scan_interval),
        BasestationInfoSensor(device, "hardware", "Hardware", "mdi:chip", scan_interval),
        BasestationInfoSensor(device, "manufacturer", "Manufacturer", "mdi:factory", scan_interval),
    ]


def _create_valve_sensors(device: ValveBasestationDevice, config: dict[str, Any]) -> list[SensorEntity]:
    """Create Valve-specific sensors."""
    entities = []

    if config["enable_info_sensors"]:
        # Add channel sensor if info sensors are enabled
        entities.append(
            BasestationInfoSensor(device, "channel", "Channel", "mdi:radio-tower", config["info_scan_interval"])
        )

    if config["enable_power_state_sensor"]:
        # Add the power state sensor for V2 devices if enabled
        entities.append(BasestationPowerStateSensor(device, config["power_state_scan_interval"]))

    return entities


def _create_vive_sensors(device: ViveBasestationDevice, config: dict[str, Any]) -> list[SensorEntity]:
    """Create Vive-specific sensors."""
    entities = []

    if device.pair_id is not None and config["enable_info_sensors"]:
        entities.append(
            BasestationInfoSensor(device, "pair_id", "Pair ID", "mdi:key-variant", config["info_scan_interval"])
        )

    return entities


class BasestationInfoSensor(SensorEntity):
    """Sensor for basestation information."""

    def __init__(
        self,
        device: BasestationDevice,
        key: "BaseStationDeviceInfoKey",
        name_suffix: str,
        icon: str,
        scan_interval: int = DEFAULT_INFO_SCAN_INTERVAL,
    ) -> None:
        """Initialize the sensor."""
        self._device = device
        self._key: BaseStationDeviceInfoKey = key
        self._scan_interval = scan_interval
        self._attr_unique_id = f"basestation_{device.mac}_{key}"
        self._attr_name = f"{device.device_name} {name_suffix}"
        self._attr_icon = icon
        self._attr_native_value = device.get_info(key, STATE_UNKNOWN)
        self._last_update = 0.0
        self._consecutive_failures = 0.0
        self._force_next_update = True

        # Share device info with main device
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device.mac)})

    @property
    def available(self) -> bool:
        """
        Return if the sensor is available.

        Info sensors show as available even when the device is not
        connected, as they represent static information.
        """
        return True

    async def async_update(self) -> None:
        """
        Update the sensor.

        Uses user-configured scan interval and adaptive behavior based on previous successes/failures.
        """
        current_time = time.time()

        # Determine if we should update
        should_update = (
            self._force_next_update
            or self._attr_native_value == STATE_UNKNOWN
            or current_time - self._last_update >= self._scan_interval
            or (
                self._consecutive_failures > 0 and current_time - self._last_update >= min(300, self._scan_interval / 2)
            )
        )

        if not should_update:
            return

        self._force_next_update = False

        try:
            # Try to read device info
            force = self._attr_native_value == STATE_UNKNOWN or self._consecutive_failures > 0

            await self._device.read_device_info(force=force)
            new_value = self._device.get_info(self._key)

            if new_value is not None:
                # Successfully got a value
                if new_value != self._attr_native_value:
                    _LOGGER.info("Sensor %s updated: %s", self._attr_name, new_value)
                self._attr_native_value = new_value
                self._consecutive_failures = 0
            else:
                # No value received
                if self._consecutive_failures == 0:
                    _LOGGER.debug("No %s value received for %s", self._key, self._device.mac)
                self._consecutive_failures += 1

            self._last_update = current_time

        except Exception as e:
            self._consecutive_failures += 1

            if self._consecutive_failures <= MAX_CONSECUTIVE_FAILURES or self._consecutive_failures % 5 == 0:
                _LOGGER.warning(
                    "Error updating info sensor %s: %s (failure %d)",
                    self._attr_name,
                    e,
                    self._consecutive_failures,
                )

            # Force next update to use a more aggressive approach if we're having repeated failures
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self._force_next_update = True

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is being removed."""
        # Info sensors share the device with other entities, so no cleanup needed


class BasestationPowerStateSensor(SensorEntity):
    """Sensor for basestation power state."""

    def __init__(self, device: BasestationDevice, scan_interval: int = DEFAULT_POWER_STATE_SCAN_INTERVAL) -> None:
        """Initialize the sensor."""
        self._device = device
        self._scan_interval = scan_interval
        self._attr_unique_id = f"basestation_{device.mac}_power_state"
        self._attr_name = f"{device.device_name} Power State"
        self._attr_icon = "mdi:power-settings"
        self._attr_native_value = STATE_UNKNOWN
        self._last_update = 0.0
        self._consecutive_failures = 0.0

        # Share device info with main device
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device.mac)})

    @property
    def available(self) -> bool:
        """Return if the device is available."""
        return self._device.available

    async def async_update(self) -> None:
        """Update the sensor using user-configured scan interval."""
        current_time = time.time()
        if current_time - self._last_update < self._scan_interval:
            return

        if isinstance(self._device, ValveBasestationDevice):
            try:
                await self._device.update()
                # Read raw power state value
                value = await self._device.get_raw_power_state()
                if value is not None:
                    # Convert numeric state to human-readable format
                    new_state = V2_STATE_DESCRIPTIONS.get(value, f"Unknown ({hex(value)})")
                    if new_state != self._attr_native_value:
                        _LOGGER.debug(
                            "Power state changed for %s: %s",
                            self._device.mac,
                            new_state,
                        )
                    self._attr_native_value = new_state
                    self._consecutive_failures = 0
                else:
                    if self._consecutive_failures == 0:
                        _LOGGER.debug("No power state received for %s", self._device.mac)
                    self._consecutive_failures += 1

                self._last_update = current_time

            except Exception as e:
                self._consecutive_failures += 1

                if self._consecutive_failures <= MAX_CONSECUTIVE_FAILURES or self._consecutive_failures % 5 == 0:
                    _LOGGER.warning(
                        "Error updating power state sensor for %s: %s (failure %d)",
                        self._device.mac,
                        e,
                        self._consecutive_failures,
                    )

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is being removed."""
        # Power state sensor shares the device with other entities, so no cleanup needed
