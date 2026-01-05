"""Sensor component for basestation integration."""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    INITIAL_RETRY_DELAY,
    MAX_INITIAL_RETRIES,
    V2_STATE_DESCRIPTIONS,
)
from .coordinator import BasestationCoordinator
from .device import BasestationDevice, ValveBasestationDevice, ViveBasestationDevice
from .utils import get_sensor_device_config

if TYPE_CHECKING:
    from .device import BaseStationDeviceInfoKey

_LOGGER = logging.getLogger(__name__)

SENSOR_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "firmware": ("Firmware", "mdi:developer-board"),
    "model": ("Model", "mdi:card-text"),
    "hardware": ("Hardware", "mdi:chip"),
    "manufacturer": ("Manufacturer", "mdi:factory"),
    "channel": ("Channel", "mdi:radio-tower"),
    "pair_id": ("Pair ID", "mdi:key-variant"),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the basestation sensors."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data is None:
        return
    device: BasestationDevice = data["device"]
    coordinator: BasestationCoordinator = data["coordinator"]

    # Config holen
    device_config = get_sensor_device_config(entry)
    if not device_config:
        return

    # Initial info read
    if device_config["enable_info_sensors"]:
        await _perform_initial_device_info_read(device)

    entities = []

    # Info Sensors (Static, slow polling)
    if device_config["enable_info_sensors"]:
        entities.extend(
            [
                BasestationInfoSensor(
                    device,
                    "firmware",
                    device_config["info_scan_interval"],
                    EntityCategory.DIAGNOSTIC,
                ),
                BasestationInfoSensor(
                    device,
                    "model",
                    device_config["info_scan_interval"],
                    EntityCategory.DIAGNOSTIC,
                ),
                BasestationInfoSensor(
                    device,
                    "hardware",
                    device_config["info_scan_interval"],
                    EntityCategory.DIAGNOSTIC,
                ),
                BasestationInfoSensor(
                    device,
                    "manufacturer",
                    device_config["info_scan_interval"],
                    EntityCategory.DIAGNOSTIC,
                ),
            ]
        )

        if isinstance(device, ValveBasestationDevice):
            entities.append(BasestationInfoSensor(device, "channel", device_config["info_scan_interval"]))
        elif isinstance(device, ViveBasestationDevice) and device.pair_id:
            entities.append(
                BasestationInfoSensor(
                    device,
                    "pair_id",
                    device_config["info_scan_interval"],
                    EntityCategory.DIAGNOSTIC,
                )
            )

    # Power State Sensor (Fast polling via Coordinator)
    if isinstance(device, ValveBasestationDevice) and device_config["enable_power_state_sensor"]:
        entities.append(BasestationPowerStateSensor(coordinator, device))

    async_add_entities(entities)


async def _perform_initial_device_info_read(device: BasestationDevice) -> None:
    """Perform initial device info read with retries."""
    for retry in range(MAX_INITIAL_RETRIES):
        try:
            if retry > 0:
                await asyncio.sleep(INITIAL_RETRY_DELAY * (retry + 1))
            if await device.read_device_info(force=True):
                break
        except Exception as err:
            _LOGGER.debug("Initial read failed (retry %s): %s", retry, err)


class BasestationInfoSensor(SensorEntity):
    """
    Sensor for static basestation information.

    Not using coordinator as this data rarely changes and doesn't need 5s polling.
    """

    def __init__(
        self,
        device: BasestationDevice,
        key: "BaseStationDeviceInfoKey",
        scan_interval: int,
        entity_category: EntityCategory | None = None,
    ) -> None:
        """Initialize the info sensor."""
        self._device = device
        self._key = key
        self._scan_interval = scan_interval
        self._attr_unique_id = f"basestation_{device.mac}_{key}"
        self._attr_has_entity_name = True
        name, icon = SENSOR_DESCRIPTIONS.get(key, (key.capitalize(), "mdi:information"))
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_category = entity_category
        self._attr_native_value = device.get_info(key, STATE_UNKNOWN)
        self._last_update = 0.0
        self._attr_device_info = {"identifiers": {(DOMAIN, device.mac)}}

    async def async_update(self) -> None:
        """Update the sensor value."""
        current_time = time.time()
        if current_time - self._last_update < self._scan_interval and self._attr_native_value != STATE_UNKNOWN:
            return

        try:
            await self._device.read_device_info(force=False)
            self._attr_native_value = self._device.get_info(self._key)
            self._last_update = current_time
        except Exception as err:
            _LOGGER.debug("Error updating info sensor %s: %s", self.name, err)


class BasestationPowerStateSensor(CoordinatorEntity, SensorEntity):
    """Sensor for basestation power state using the DataUpdateCoordinator."""

    def __init__(self, coordinator: BasestationCoordinator, device: BasestationDevice) -> None:
        """Initialize the power state sensor."""
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"basestation_{device.mac}_power_state"
        self._attr_has_entity_name = True
        self._attr_name = "Power State"
        self._attr_icon = "mdi:power-settings"
        self._attr_device_info = {"identifiers": {(DOMAIN, device.mac)}}

    @property
    def native_value(self) -> str:
        """Return the state based on coordinator data."""
        # Coordinator calls device.update(), so device.last_power_state is fresh
        val = self._device.last_power_state
        if val is None:
            return STATE_UNKNOWN
        return V2_STATE_DESCRIPTIONS.get(val, f"Unknown ({hex(val)})")
