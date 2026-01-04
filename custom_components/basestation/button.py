"""
Button component for basestation integration.

This module has been optimized to reduce BLE polling. The identify button
now determines its availability based on whether we have recent power state
data from the Power State sensor.
"""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .device import BasestationDevice, ValveBasestationDevice, get_basestation_device
from .utils import get_basic_device_config

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the basestation button."""
    _LOGGER.debug("Setting up basestation button entities for entry: %s", entry.title)

    # Get device configuration from the config entry using shared utility
    device_config = get_basic_device_config(entry)
    if not device_config:
        return

    # Create the basestation device instance
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

    # Only add the identify button for Valve basestations (V2 devices)
    # V1 basestations don't support the identify function
    if isinstance(device, ValveBasestationDevice):
        entities = [BasestationIdentifyButton(device)]
        async_add_entities(entities, update_before_add=True)
        _LOGGER.info(
            "Successfully added identify button for %s setup: %s",
            device_config["setup_method"],
            device_config["name"] or device_config["mac"],
        )
    else:
        _LOGGER.debug("Skipping identify button for V1 device: %s", device_config["mac"])


class BasestationIdentifyButton(ButtonEntity):
    """
    Button to identify the basestation by blinking its LED.

    Availability is determined by whether we have recent power state data,
    which indicates the device is connected and reachable.
    """

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
        """
        Return if the button is available.

        The button is available when we have fresh power state data,
        indicating the device is connected and reachable.
        """
        # For V2 devices, check if we have fresh state from the Power State sensor
        if isinstance(self._device, ValveBasestationDevice):
            return self._device.has_fresh_state

        # For V1 devices (shouldn't happen as we only create this for V2), use device availability
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
        """
        Update the button state including availability.

        No polling is needed - availability is derived from the device's
        cached power state which is maintained by the Power State sensor.
        """
        # No polling needed - availability is automatically updated via property

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is being removed."""
        # Button shares the device with other entities, so no cleanup needed
