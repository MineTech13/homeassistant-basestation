"""The basestation switch component."""

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BasestationCoordinator
from .device import BasestationDevice, ValveBasestationDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the basestation switch from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    device: BasestationDevice = data["device"]
    coordinator: BasestationCoordinator = data["coordinator"]

    entities = [BasestationSwitch(coordinator, device)]

    if isinstance(device, ValveBasestationDevice):
        entities.append(BasestationStandbySwitch(coordinator, device))

    async_add_entities(entities)


class BasestationSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a basestation main power switch."""

    def __init__(self, coordinator: BasestationCoordinator, device: BasestationDevice) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"basestation_{device.mac}"
        self._attr_has_entity_name = True
        self._attr_name = None
        self._attr_icon = "mdi:virtual-reality"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.mac)},
            "name": device.device_name,
            "manufacturer": "Valve" if isinstance(device, ValveBasestationDevice) else "HTC",
            "model": "Index Basestation" if isinstance(device, ValveBasestationDevice) else "Vive Basestation",
            "serial_number": device.mac,
        }

    @property
    def is_on(self) -> bool:
        """Return if the switch is currently on or off."""
        if isinstance(self._device, ValveBasestationDevice):
            if self._device.last_power_state is None:
                return False
            return self._device.last_power_state != 0x00
        return self._device.is_on

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn the switch on."""
        await self._device.turn_on()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn the switch off."""
        await self._device.turn_off()
        await self.coordinator.async_request_refresh()


class BasestationStandbySwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a basestation standby switch (V2 only)."""

    def __init__(self, coordinator: BasestationCoordinator, device: BasestationDevice) -> None:
        """Initialize the standby switch."""
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"basestation_{device.mac}_standby"
        self._attr_has_entity_name = True
        self._attr_name = "Standby Mode"
        self._attr_icon = "mdi:sleep"
        self._attr_device_info = {"identifiers": {(DOMAIN, device.mac)}}

    @property
    def is_on(self) -> bool:
        """Return if the standby mode is active."""
        if isinstance(self._device, ValveBasestationDevice):
            return self._device.is_in_standby
        return False

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn on standby mode."""
        if isinstance(self._device, ValveBasestationDevice):
            await self._device.set_standby()
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn off standby mode (turn fully on)."""
        if isinstance(self._device, ValveBasestationDevice):
            await self._device.turn_on()
            await self.coordinator.async_request_refresh()
