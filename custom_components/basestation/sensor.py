"""Sensor component for basestation integration."""

import logging
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    V2_STATE_DESCRIPTIONS,
)
from .coordinator import BasestationCoordinator, BasestationInfoCoordinator
from .device import BasestationDevice, ValveBasestationDevice, ViveBasestationDevice

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
    info_coordinator: BasestationInfoCoordinator = data["info_coordinator"]

    entities: list[SensorEntity] = []

    # Info Sensors (Static, slow polling via info_coordinator)
    entities.extend(
        [
            BasestationInfoSensor(info_coordinator, device, "firmware", EntityCategory.DIAGNOSTIC),
            BasestationInfoSensor(info_coordinator, device, "model", EntityCategory.DIAGNOSTIC),
            BasestationInfoSensor(info_coordinator, device, "hardware", EntityCategory.DIAGNOSTIC),
            BasestationInfoSensor(info_coordinator, device, "manufacturer", EntityCategory.DIAGNOSTIC),
        ]
    )

    if isinstance(device, ValveBasestationDevice):
        entities.append(BasestationInfoSensor(info_coordinator, device, "channel"))
    elif isinstance(device, ViveBasestationDevice) and device.pair_id:
        entities.append(BasestationInfoSensor(info_coordinator, device, "pair_id", EntityCategory.DIAGNOSTIC))

    # Power State Sensor (Fast polling via Coordinator)
    if isinstance(device, ValveBasestationDevice):
        entities.append(BasestationPowerStateSensor(coordinator, device))

    async_add_entities(entities)


class BasestationInfoSensor(CoordinatorEntity, SensorEntity):
    """Sensor for static basestation information using the InfoCoordinator."""

    def __init__(
        self,
        coordinator: BasestationInfoCoordinator,
        device: BasestationDevice,
        key: "BaseStationDeviceInfoKey",
        entity_category: EntityCategory | None = None,
    ) -> None:
        """Initialize the info sensor."""
        super().__init__(coordinator)
        self._device = device
        self._key: BaseStationDeviceInfoKey = key
        self._attr_unique_id = f"basestation_{device.mac}_{key}"
        self._attr_has_entity_name = True
        self._attr_translation_key = key
        _, icon = SENSOR_DESCRIPTIONS.get(key, (key.capitalize(), "mdi:information"))
        self._attr_icon = icon
        self._attr_entity_category = entity_category
        self._attr_device_info = device.device_info

    @property
    def native_value(self) -> str | None:
        """Return the state based on info coordinator data."""
        if not self.coordinator.data:
            return self._device.get_info(self._key, STATE_UNKNOWN)
        return self.coordinator.data.get(self._key, STATE_UNKNOWN)


class BasestationPowerStateSensor(CoordinatorEntity, SensorEntity):
    """Sensor for basestation power state using the DataUpdateCoordinator."""

    def __init__(self, coordinator: BasestationCoordinator, device: BasestationDevice) -> None:
        """Initialize the power state sensor."""
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"basestation_{device.mac}_power_state"
        self._attr_has_entity_name = True
        self._attr_translation_key = "power_state"
        self._attr_icon = "mdi:power-settings"
        self._attr_device_info = device.device_info

    @property
    def native_value(self) -> str:
        """Return the state based on coordinator data."""
        val = self._device.last_power_state
        if val is None:
            return STATE_UNKNOWN
        return V2_STATE_DESCRIPTIONS.get(val, f"Unknown ({hex(val) if isinstance(val, int) else val})")
