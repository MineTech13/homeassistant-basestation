"""Button component for basestation integration."""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_TYPE,
    CONF_PAIR_ID,
    DOMAIN,
)
from .device import BasestationDevice, ValveBasestationDevice, get_basestation_device

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the basestation button."""
    _LOGGER.debug("Setting up basestation button entities for entry: %s", entry.title)

    # Get device configuration from the config entry
    mac = entry.data.get(CONF_MAC)
    name = entry.data.get(CONF_NAME)
    device_type = entry.data.get(CONF_DEVICE_TYPE)
    pair_id = entry.data.get(CONF_PAIR_ID)
    setup_method = entry.data.get("setup_method", "unknown")

    if not mac:
        _LOGGER.error("No MAC address found in config entry data: %s", entry.data)
        return

    _LOGGER.debug("Creating device for MAC: %s, Name: %s, Type: %s, Method: %s", mac, name, device_type, setup_method)

    # Create the basestation device instance
    device = get_basestation_device(hass, mac, name=name, device_type=device_type, pair_id=pair_id)

    if not device:
        _LOGGER.error("Failed to create device for MAC: %s", mac)
        return

    # Only add the identify button for Valve basestations (V2 devices)
    # V1 basestations don't support the identify function
    if isinstance(device, ValveBasestationDevice):
        entities = [BasestationIdentifyButton(device)]
        async_add_entities(entities, update_before_add=True)
        _LOGGER.info("Successfully added identify button for %s setup: %s", setup_method, name or mac)
    else:
        _LOGGER.debug("Skipping identify button for V1 device: %s", mac)


class BasestationIdentifyButton(ButtonEntity):
    """Button to identify the basestation by blinking its LED."""

    def __init__(self, device: BasestationDevice) -> None:
        """Initialize the identify button."""
        self._device = device
        self._attr_unique_id = f"basestation_{device.mac}_identify"
        self._attr_name = f"{device.device_name} Identify"
        self._attr_icon = "mdi:lightbulb-flash"
        self._pressed = False

        # Share device info with main device registry entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
        )

        _LOGGER.debug("Initialized BasestationIdentifyButton for %s (%s)", device.device_name, device.mac)

    @property
    def available(self) -> bool:
        """Return if the device is available."""
        return self._device.available

    async def async_press(self) -> None:
        """Handle the button press to identify the basestation."""
        if isinstance(self._device, ValveBasestationDevice):
            _LOGGER.info("Identify button pressed for %s", self._device.mac)
            self._pressed = True

            try:
                # Call the identify function which will make the basestation LED blink
                await self._device.identify()
                _LOGGER.debug("Identify command sent successfully to %s", self._device.mac)
            except Exception:
                _LOGGER.exception("Failed to send identify command to %s", self._device.mac)
            finally:
                self._pressed = False

    async def async_update(self) -> None:
        """Update the button state including availability."""
        # This method ensures the button state is updated when Home Assistant polls entities
        # It primarily updates the availability status based on device connectivity
        await self._device.update()
