"""Button component for basestation integration."""
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.const import CONF_MAC, CONF_NAME

from .const import (
    DOMAIN, 
    CONF_DEVICE_TYPE, 
    CONF_PAIR_ID,
    DEVICE_TYPE_V1,
    DEVICE_TYPE_V2,
)
from .device import get_basestation_device, ValveBasestationDevice

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the basestation button."""
    # Get config entry data
    if entry.data.get("setup_method") == "automatic":
        # For automatic setup, create entities for each discovered device
        devices = entry.data.get("devices", [])
        entities = []
        
        for device_data in devices:
            mac = device_data[CONF_MAC]
            name = device_data.get(CONF_NAME)
            device_type = device_data.get(CONF_DEVICE_TYPE, entry.data.get(CONF_DEVICE_TYPE))
            pair_id = device_data.get(CONF_PAIR_ID)
            
            device_info = {
                "name": name,
                "device_type": device_type,
                "pair_id": pair_id,
            }
            
            device = get_basestation_device(hass, mac, device_info)
            # Only add the identify button for Valve basestations
            if isinstance(device, ValveBasestationDevice):
                entities.append(BasestationIdentifyButton(device))
        
        if entities:
            async_add_entities(entities, update_before_add=True)
    else:
        # For manual or selection setup, create entity for the single device
        mac = entry.data[CONF_MAC]
        name = entry.data.get(CONF_NAME)
        device_type = entry.data.get(CONF_DEVICE_TYPE)
        pair_id = entry.data.get(CONF_PAIR_ID)
        
        device_info = {
            "name": name,
            "device_type": device_type,
            "pair_id": pair_id,
        }
        
        device = get_basestation_device(hass, mac, device_info)
        # Only add the identify button for Valve basestations
        if isinstance(device, ValveBasestationDevice):
            async_add_entities([BasestationIdentifyButton(device)], update_before_add=True)


class BasestationIdentifyButton(ButtonEntity):
    """Button to identify the basestation by blinking its LED."""

    def __init__(self, device):
        """Initialize the button."""
        self._device = device
        self._attr_unique_id = f"basestation_{device.mac}_identify"
        self._attr_name = f"{device.device_name} Identify"
        self._attr_icon = "mdi:lightbulb-flash"
        self._pressed = False
        
        # Share device info with main switch
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
        )

    @property
    def available(self):
        """Return if the device is available."""
        return self._device._available

    async def async_press(self) -> None:
        """Handle the button press."""
        if isinstance(self._device, ValveBasestationDevice):
            _LOGGER.info("Identify button pressed for %s", self._device.mac)
            self._pressed = True
            await self._device.identify()
            self._pressed = False
    
    async def async_update(self) -> None:
        """Update the button state including availability."""
        # This method ensures the button state is updated when Home Assistant polls entities
        await self._device.update()