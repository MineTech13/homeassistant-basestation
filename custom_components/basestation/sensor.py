"""Sensor component for basestation integration."""
import logging
import time
from typing import Optional

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.const import CONF_MAC, CONF_NAME, STATE_UNKNOWN

from .const import (
    DOMAIN, 
    CONF_DEVICE_TYPE, 
    CONF_PAIR_ID,
    # Power state descriptions
    V2_STATE_DESCRIPTIONS,
    # Scan intervals
    INFO_SENSOR_SCAN_INTERVAL,
    POWER_STATE_SCAN_INTERVAL,
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
        
        # First try to read device info with a reliable connection
        try:
            # Force initial read to ensure we get the data
            await device.read_device_info(force=True)
        except Exception as e:
            _LOGGER.warning(f"Error reading initial device info for {mac}: {e}")
        
        # Always add the firmware sensor - it will handle unavailability gracefully
        entities.append(BasestationInfoSensor(
            device, "firmware", "Firmware", "mdi:developer-board"
        ))
        
        # Always add other core sensors too
        entities.append(BasestationInfoSensor(
            device, "model", "Model", "mdi:card-text"
        ))
        
        entities.append(BasestationInfoSensor(
            device, "hardware", "Hardware", "mdi:chip"
        ))
        
        entities.append(BasestationInfoSensor(
            device, "manufacturer", "Manufacturer", "mdi:factory"
        ))
        
        # V2-specific sensors
        if isinstance(device, ValveBasestationDevice):
            # Add channel sensor if present
            entities.append(BasestationInfoSensor(
                device, "channel", "Channel", "mdi:radio-tower"
            ))
            
            # Add the power state sensor for V2 devices
            entities.append(BasestationPowerStateSensor(device))
        
        # V1-specific sensors
        if isinstance(device, ViveBasestationDevice) and "pair_id" in device._info:
            entities.append(BasestationInfoSensor(
                device, "pair_id", "Pair ID", "mdi:key-variant"
            ))
    
    if entities:
        async_add_entities(entities, update_before_add=True)


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
        self._last_update = 0
        
        # Share device info with main device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
        )

    @property
    def available(self):
        """Return if the device is available.
        Even if the device is unavailable, we return True for info sensors
        as they represent static device information.
        """
        return True

    async def async_update(self) -> None:
        """Update the sensor.
        
        Only updates at the defined scan interval to reduce BLE traffic.
        """
        current_time = time.time()
        if current_time - self._last_update < INFO_SENSOR_SCAN_INTERVAL:
            return
            
        try:
            # Don't force read - use cached info if available
            await self._device.read_device_info(force=False)
            self._attr_native_value = self._device._info.get(self._key, STATE_UNKNOWN)
            self._last_update = current_time
        except Exception as e:
            _LOGGER.debug(f"Error updating info sensor {self._attr_name}: {e}")


class BasestationPowerStateSensor(SensorEntity):
    """Sensor for basestation power state."""
    
    def __init__(self, device):
        """Initialize the sensor."""
        self._device = device
        self._attr_unique_id = f"basestation_{device.mac}_power_state"
        self._attr_name = f"{device.device_name} Power State"
        self._attr_icon = "mdi:power-settings"
        self._attr_native_value = STATE_UNKNOWN
        self._last_update = 0
        
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
        current_time = time.time()
        if current_time - self._last_update < POWER_STATE_SCAN_INTERVAL:
            return
            
        if isinstance(self._device, ValveBasestationDevice):
            try:
                await self._device.update()
                # Read raw power state value
                value = await self._device.get_raw_power_state()
                if value is not None:
                    # Convert numeric state to human-readable format
                    self._attr_native_value = V2_STATE_DESCRIPTIONS.get(
                        value, f"Unknown ({hex(value)})"
                    )
                else:
                    self._attr_native_value = STATE_UNKNOWN
                self._last_update = current_time
            except Exception as e:
                _LOGGER.debug(f"Error updating power state sensor: {e}")
                self._attr_native_value = STATE_UNKNOWN