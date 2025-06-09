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
    CONF_DEVICE_TYPE,
    CONF_PAIR_ID,
    DOMAIN,
    # Scan intervals
    INFO_SENSOR_SCAN_INTERVAL,
    INITIAL_RETRY_DELAY,
    MAX_CONSECUTIVE_FAILURES,
    MAX_INITIAL_RETRIES,
    POWER_STATE_SCAN_INTERVAL,
    # Power state descriptions
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
    # Get config entry data
    if entry.data.get("setup_method") == "automatic":
        # For automatic setup, create entities for each discovered device
        devices = entry.data.get("devices", [])
        await _setup_sensors_for_devices(hass, devices, async_add_entities)
    else:
        # For manual or selection setup, create entity for the single device
        device_data = {
            CONF_MAC: entry.data[CONF_MAC],
            CONF_NAME: entry.data.get(CONF_NAME),
            CONF_DEVICE_TYPE: entry.data.get(CONF_DEVICE_TYPE),
            CONF_PAIR_ID: entry.data.get(CONF_PAIR_ID),
        }
        await _setup_sensors_for_devices(hass, [device_data], async_add_entities)


async def _setup_sensors_for_devices(
    hass: HomeAssistant,
    devices: list[dict[str, Any]],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for a list of devices."""
    entities: list[SensorEntity] = []

    for device_data in devices:
        mac = device_data[CONF_MAC]
        name = device_data.get(CONF_NAME)
        device_type = device_data.get(CONF_DEVICE_TYPE)
        pair_id = device_data.get(CONF_PAIR_ID)

        _LOGGER.info("Setting up sensors for device: %s (%s)", name or mac, mac)

        device = get_basestation_device(hass, mac, name=name, device_type=device_type, pair_id=pair_id)
        # Try initial device info read with retries
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

        entities.extend(
            (
                # Always add the firmware sensor - it will handle unavailability gracefully
                BasestationInfoSensor(device, "firmware", "Firmware", "mdi:developer-board"),
                # Always add other core sensors too
                BasestationInfoSensor(device, "model", "Model", "mdi:card-text"),
                BasestationInfoSensor(device, "hardware", "Hardware", "mdi:chip"),
                BasestationInfoSensor(device, "manufacturer", "Manufacturer", "mdi:factory"),
            )
        )

        # V2-specific sensors
        if isinstance(device, ValveBasestationDevice):
            entities.extend(
                [
                    # Add channel sensor if present
                    BasestationInfoSensor(device, "channel", "Channel", "mdi:radio-tower"),
                    # Add the power state sensor for V2 devices
                    BasestationPowerStateSensor(device),
                ]
            )

        # V1-specific sensors
        if isinstance(device, ViveBasestationDevice) and pair_id is not None:
            entities.append(BasestationInfoSensor(device, "pair_id", "Pair ID", "mdi:key-variant"))

    if entities:
        async_add_entities(entities, update_before_add=True)


class BasestationInfoSensor(SensorEntity):
    """Sensor for basestation information."""

    def __init__(self, device: BasestationDevice, key: "BaseStationDeviceInfoKey", name_suffix: str, icon: str) -> None:
        """Initialize the sensor."""
        self._device = device
        self._key: BaseStationDeviceInfoKey = key
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

        Uses adaptive scan interval based on previous successes/failures.
        """
        current_time = time.time()

        # Determine if we should update:
        # 1. If forced
        # 2. If we don't have a value yet
        # 3. If regular scan interval has passed
        # 4. If we've had failures and are in accelerated retry mode
        should_update = (
            self._force_next_update
            or self._attr_native_value == STATE_UNKNOWN
            or current_time - self._last_update >= INFO_SENSOR_SCAN_INTERVAL
            or (
                self._consecutive_failures > 0
                and current_time - self._last_update >= min(300, INFO_SENSOR_SCAN_INTERVAL / 2)
            )
        )

        if not should_update:
            return

        self._force_next_update = False

        try:
            # Try to read device info with or without forcing based on current sensor state
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
                # Log more frequently for initial failures, then just every 5th failure
                _LOGGER.warning(
                    "Error updating info sensor %s: %s (failure %d)",
                    self._attr_name,
                    e,
                    self._consecutive_failures,
                )

            # Force next update to use a more aggressive approach if we're having repeated failures
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self._force_next_update = True


class BasestationPowerStateSensor(SensorEntity):
    """Sensor for basestation power state."""

    def __init__(self, device: BasestationDevice) -> None:
        """Initialize the sensor."""
        self._device = device
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
        """Update the sensor."""
        current_time = time.time()
        if current_time - self._last_update < POWER_STATE_SCAN_INTERVAL:
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
