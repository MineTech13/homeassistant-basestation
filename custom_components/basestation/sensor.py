"""Sensor component for basestation integration."""
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.const import CONF_MAC, CONF_NAME

from .const import (
    DOMAIN, 
    CONF_DEVICE_TYPE, 
    CONF_PAIR_ID,
)
from .device import get_basestation_device, ValveBasestationDevice, ViveBasestationDevice

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


async def _setup_sensors_for_devices(hass, devices, async_add_entities):
    """Set up sensors for a list of devices."""
    entities = []
    
    for device_data in devices:
        mac = device_data[CONF_MAC]
        name = device_data.get(CONF_NAME)
        device_type = device_data.get(CONF_DEVICE_TYPE)
        pair_id = device_data.get(CONF_PAIR_ID)
        
        device_info = {
            "name": name,
            "device_type": device_type,
            "pair_id": pair_id,
        }
        
        device = get_basestation_device(hass, mac, device_info)
        
        # First read device info
        await device.read_device_info()
        
        # Create sensors based on available info
        if "firmware" in device._info:
            entities.append(BasestationInfoSensor(
                device, "firmware", "Firmware", "mdi:developer-board"
            ))
        
        if "model" in device._info:
            entities.append(BasestationInfoSensor(
                device, "model", "Model", "mdi:card-text"
            ))
        
        if "hardware" in device._info:
            entities.append(BasestationInfoSensor(
                device, "hardware", "Hardware", "mdi:chip"
            ))
        
        if "manufacturer" in device._info:
            entities.append(BasestationInfoSensor(
                device, "manufacturer", "Manufacturer", "mdi:factory"
            ))
        
        # V2-specific sensors
        if isinstance(device, ValveBasestationDevice) and "channel" in device._info:
            entities.append(BasestationInfoSensor(
                device, "channel", "Channel", "mdi:radio-tower"
            ))
        
        # V1-specific sensors
        if isinstance(device, ViveBasestationDevice) and "pair_id" in device._info:
            entities.append(BasestationInfoSensor(
                device, "pair_id", "Pair ID", "mdi:key-variant"
            ))
    
    if entities:
        async_add_entities(entities)


class BasestationInfoSensor(SensorEntity):
    """Sensor for basestation information."""

    def __init__(self, device, key, name_suffix, icon):
        """Initialize the sensor."""
        self._device = device
        self._key = key
        self._attr_unique_id = f"basestation_{device.mac}_{key}"
        self._attr_name = f"{device.device_name} {name_suffix}"
        self._attr_icon = icon
        self._attr_native_value = device._info.get(key)
        
        # Share device info with main device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
        )

    @property
    def available(self):
        """Return if the device is available."""
        return self._device._available

    async def async_update(self) -> None:
        """Update the sensor."""
        await self._device.read_device_info()
        self._attr_native_value = self._device._info.get(self._key)