"""Sensor component for basestation integration."""

import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEFAULT_INFO_SCAN_INTERVAL,
    DOMAIN,
    INITIAL_RETRY_DELAY,
    MAX_CONSECUTIVE_FAILURES,
    MAX_INITIAL_RETRIES,
    V2_STATE_DESCRIPTIONS,
)
from .coordinator import BasestationCoordinator
from .device import BasestationDevice, ValveBasestationDevice, ViveBasestationDevice
from .utils import get_sensor_device_config

if TYPE_CHECKING:
    from .device import BaseStationDeviceInfoKey

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the basestation sensors."""
    data = hass.data[DOMAIN][entry.entry_id]
    device: BasestationDevice = data["device"]
    coordinator: BasestationCoordinator = data["coordinator"]

    # Config holen
    device_config = get_sensor_device_config(entry)
    if not device_config:
        return

    # Initial info read
    if device_config["enable_info_sensors"]:
        await _perform_initial_device_info_read(device, device.mac)

    entities = []

    # Info Sensors (Static, slow polling)
    if device_config["enable_info_sensors"]:
        entities.extend(
            [
                BasestationInfoSensor(
                    device, "firmware", "Firmware", "mdi:developer-board", device_config["info_scan_interval"]
                ),
                BasestationInfoSensor(device, "model", "Model", "mdi:card-text", device_config["info_scan_interval"]),
                BasestationInfoSensor(device, "hardware", "Hardware", "mdi:chip", device_config["info_scan_interval"]),
                BasestationInfoSensor(
                    device, "manufacturer", "Manufacturer", "mdi:factory", device_config["info_scan_interval"]
                ),
            ]
        )

        if isinstance(device, ValveBasestationDevice):
            entities.append(
                BasestationInfoSensor(
                    device, "channel", "Channel", "mdi:radio-tower", device_config["info_scan_interval"]
                )
            )
        elif isinstance(device, ViveBasestationDevice) and device.pair_id:
            entities.append(
                BasestationInfoSensor(
                    device, "pair_id", "Pair ID", "mdi:key-variant", device_config["info_scan_interval"]
                )
            )

    # Power State Sensor (Fast polling via Coordinator)
    if isinstance(device, ValveBasestationDevice) and device_config["enable_power_state_sensor"]:
        entities.append(BasestationPowerStateSensor(coordinator, device))

    async_add_entities(entities)


async def _perform_initial_device_info_read(device: BasestationDevice, mac: str) -> None:
    """Perform initial device info read with retries."""
    for retry in range(MAX_INITIAL_RETRIES):
        try:
            if retry > 0:
                await asyncio.sleep(INITIAL_RETRY_DELAY * (retry + 1))
            if await device.read_device_info(force=True):
                break
        except Exception:
            pass


class BasestationInfoSensor(SensorEntity):
    """
    Sensor for static basestation information.
    Not using coordinator as this data rarely changes and doesn't need 5s polling.
    """

    def __init__(
        self,
        device: BasestationDevice,
        key: "BaseStationDeviceInfoKey",
        name_suffix: str,
        icon: str,
        scan_interval: int,
    ) -> None:
        self._device = device
        self._key = key
        self._scan_interval = scan_interval
        self._attr_unique_id = f"basestation_{device.mac}_{key}"
        self._attr_name = f"{device.device_name} {name_suffix}"
        self._attr_icon = icon
        self._attr_native_value = device.get_info(key, STATE_UNKNOWN)
        self._last_update = 0.0
        self._attr_device_info = {"identifiers": {(DOMAIN, device.mac)}}

    async def async_update(self) -> None:
        current_time = time.time()
        if current_time - self._last_update < self._scan_interval and self._attr_native_value != STATE_UNKNOWN:
            return

        try:
            await self._device.read_device_info(force=False)
            self._attr_native_value = self._device.get_info(self._key)
            self._last_update = current_time
        except Exception:
            pass


class BasestationPowerStateSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor for basestation power state using the DataUpdateCoordinator.
    """

    def __init__(self, coordinator: BasestationCoordinator, device: BasestationDevice) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"basestation_{device.mac}_power_state"
        self._attr_name = f"{device.device_name} Power State"
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
