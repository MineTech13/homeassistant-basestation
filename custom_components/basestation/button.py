"""Button component for basestation integration."""

import logging

from homeassistant.components.button import ButtonEntity
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
    """Set up the basestation button."""
    data = hass.data[DOMAIN][entry.entry_id]
    device: BasestationDevice = data["device"]
    coordinator: BasestationCoordinator = data["coordinator"]

    if isinstance(device, ValveBasestationDevice):
        async_add_entities([BasestationIdentifyButton(coordinator, device)])


class BasestationIdentifyButton(CoordinatorEntity, ButtonEntity):
    """Button to identify the basestation by blinking its LED."""

    def __init__(self, coordinator: BasestationCoordinator, device: BasestationDevice) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"basestation_{device.mac}_identify"
        self._attr_name = f"{device.device_name} Identify"
        self._attr_icon = "mdi:lightbulb-flash"
        self._attr_device_info = {"identifiers": {(DOMAIN, device.mac)}}

    async def async_press(self) -> None:
        """Handle the button press."""
        if isinstance(self._device, ValveBasestationDevice):
            await self._device.identify()
