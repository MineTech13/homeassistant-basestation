"""The basestation switch component."""
import logging
from datetime import datetime, timedelta

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
from .device import (
    get_basestation_device, 
    ViveBasestationDevice, 
    ValveBasestationDevice
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the basestation switch."""
    # Get config entry data
    if entry.data.get("setup_method") == "automatic":
        # For automatic setup, create entities for each discovered device
        devices = entry.data.get("devices", [])
        _LOGGER.debug("Setting up %s automatically discovered devices", len(devices))
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
            if device:
                entities.append(BasestationSwitch(device, entry.entry_id))
                
                # Add standby switch for Valve basestations
                if isinstance(device, ValveBasestationDevice):
                    entities.append(BasestationStandbySwitch(device, entry.entry_id))
        
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
        if device:
            entities = [BasestationSwitch(device, entry.entry_id)]
            
            # Add standby switch for Valve basestations
            if isinstance(device, ValveBasestationDevice):
                entities.append(BasestationStandbySwitch(device, entry.entry_id))
                
            async_add_entities(entities, update_before_add=True)


class BasestationSwitch(SwitchEntity):
    """Representation of a basestation switch."""

    def __init__(self, device, entry_id):
        """Initialize the switch."""
        self._device = device
        self._entry_id = entry_id
        self._attr_unique_id = f"basestation_{device.mac}"
        self._attr_name = device.device_name
        self._attr_icon = "mdi:virtual-reality"
        
        # Create device info for Home Assistant device registry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
            name=device.device_name,
            manufacturer="Valve" if isinstance(device, ValveBasestationDevice) else "HTC",
            model="Index Basestation" if isinstance(device, ValveBasestationDevice) else "Vive Basestation",
        )

    @property
    def is_on(self):
        """Return if the switch is currently on or off."""
        return self._device._is_on

    @property
    def available(self):
        """Return if the device is available."""
        return self._device._available

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        await self._device.turn_on()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        await self._device.turn_off()
        self.async_write_ha_state()

    async def async_update(self):
        """Fetch new state data for the sensor."""
        await self._device.update()


class BasestationStandbySwitch(SwitchEntity):
    """Representation of a basestation standby switch (V2 only)."""

    def __init__(self, device, entry_id):
        """Initialize the switch."""
        self._device = device
        self._entry_id = entry_id
        self._attr_unique_id = f"basestation_{device.mac}_standby"
        self._attr_name = f"{device.device_name} Standby Mode"
        self._attr_icon = "mdi:sleep"
        self._is_in_standby = False
        
        # Share device info with main switch
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
        )

    @property
    def is_on(self):
        """Return if standby mode is active."""
        return self._is_in_standby

    @property
    def available(self):
        """Return if the device is available."""
        return self._device._available

    async def async_turn_on(self, **kwargs):
        """Turn on standby mode (instead of full sleep)."""
        if isinstance(self._device, ValveBasestationDevice):
            await self._device.set_standby()
            self._is_in_standby = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn off standby mode (device will go to full on mode)."""
        if isinstance(self._device, ValveBasestationDevice):
            await self._device.turn_on()
            self._is_in_standby = False
            self.async_write_ha_state()

    async def async_update(self):
        """Update the standby state based on device state."""
        # We don't have a direct way to determine if in standby mode
        # This would require an additional read of the characteristic
        pass