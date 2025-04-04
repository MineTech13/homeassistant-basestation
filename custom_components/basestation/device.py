"""Device classes for basestation integration."""
import logging
import asyncio
import struct
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

from bleak import BleakClient, BleakError
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import (
    V2_PWR_CHARACTERISTIC,
    V2_IDENTIFY_CHARACTERISTIC,
    V2_CHANNEL_CHARACTERISTIC,
    V2_PWR_ON,
    V2_PWR_STANDBY,
    V2_PWR_SLEEP,
    V1_PWR_CHARACTERISTIC,
    FIRMWARE_CHARACTERISTIC,
    MODEL_CHARACTERISTIC,
    HARDWARE_CHARACTERISTIC,
    MANUFACTURER_CHARACTERISTIC,
    V1_NAME_PREFIX,
    V2_NAME_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

# Limit concurrent connections
CONNECTION_SEMAPHORE = asyncio.Semaphore(2)
CONNECTION_DELAY = 0.5  # Delay between connections in seconds
CONNECTION_TIMEOUT = 10  # Connection timeout in seconds
MAX_RETRIES = 2  # Maximum connection retry attempts

class BasestationDevice(ABC):
    """Base class for basestation devices."""

    def __init__(self, hass: HomeAssistant, mac: str, name: Optional[str] = None):
        """Initialize the device."""
        self.hass = hass
        self.mac = mac
        self.custom_name = name
        self._is_on = False
        self._available = False
        self._info = {}
        self._retry_count = 0

    @property
    def device_name(self) -> str:
        """Return the name of the device."""
        if self.custom_name:
            return self.custom_name
        return self.default_name

    @property
    @abstractmethod
    def default_name(self) -> str:
        """Return the default name for this device type."""
        pass

    @abstractmethod
    async def turn_on(self) -> None:
        """Turn on the device."""
        pass

    @abstractmethod
    async def turn_off(self) -> None:
        """Turn off the device."""
        pass

    @abstractmethod
    async def update(self) -> None:
        """Update the device state."""
        pass

    def get_ble_device(self):
        """Get the BLE device from the address."""
        return bluetooth.async_ble_device_from_address(self.hass, self.mac)

    async def async_ble_operation(self, operation_type: str, characteristic_uuid: str, 
                                 retry: bool = True, value: bytes = None):
        """Execute a BLE operation with proper connection management."""
        result = None
        
        for attempt in range(MAX_RETRIES if retry else 1):
            try:
                async with CONNECTION_SEMAPHORE:
                    # Add delay to prevent overwhelming the BLE adapter
                    if attempt > 0:
                        await asyncio.sleep(CONNECTION_DELAY * (2 ** attempt))
                    else:
                        await asyncio.sleep(CONNECTION_DELAY)
                    
                    # Get BLE device from registry
                    device = self.get_ble_device()
                    if not device:
                        _LOGGER.debug("Device %s not found in Bluetooth registry", self.mac)
                        continue

                    # Connect to device and execute operation
                    async with BleakClient(device, timeout=CONNECTION_TIMEOUT) as client:
                        if operation_type == "write":
                            await client.write_gatt_char(characteristic_uuid, value)
                            result = True
                        elif operation_type == "read":
                            result = await client.read_gatt_char(characteristic_uuid)
                        
                        # Reset retry count on success
                        self._retry_count = 0
                        self._available = True
                        return result
                        
            except BleakError as err:
                _LOGGER.debug(
                    "BLE error on basestation '%s' (attempt %d/%d): %s",
                    self.mac,
                    attempt + 1,
                    MAX_RETRIES if retry else 1,
                    str(err),
                )
                
            except Exception as ex:
                _LOGGER.debug(
                    "Failed to execute %s on basestation '%s': %s",
                    operation_type,
                    self.mac,
                    str(ex),
                )
                
            # Increment retry count
            self._retry_count += 1
            
        # If we get here, all attempts failed
        self._available = False
        return False

    async def read_device_info(self) -> Dict[str, Any]:
        """Read device information characteristics."""
        info = {}
        try:
            device = self.get_ble_device()
            if not device:
                _LOGGER.debug("Device %s not found in Bluetooth registry", self.mac)
                return info
                
            async with BleakClient(device, timeout=CONNECTION_TIMEOUT) as client:
                # Try to read each characteristic, ignoring errors
                try:
                    firmware = await client.read_gatt_char(FIRMWARE_CHARACTERISTIC)
                    info["firmware"] = firmware.decode('utf-8').strip()
                except Exception as e:
                    _LOGGER.debug("Failed to read firmware: %s", e)

                try:
                    model = await client.read_gatt_char(MODEL_CHARACTERISTIC)
                    info["model"] = model.decode('utf-8').strip()
                except Exception as e:
                    _LOGGER.debug("Failed to read model: %s", e)

                try:
                    hardware = await client.read_gatt_char(HARDWARE_CHARACTERISTIC)
                    info["hardware"] = hardware.decode('utf-8').strip()
                except Exception as e:
                    _LOGGER.debug("Failed to read hardware: %s", e)

                try:
                    manufacturer = await client.read_gatt_char(MANUFACTURER_CHARACTERISTIC)
                    info["manufacturer"] = manufacturer.decode('utf-8').strip()
                except Exception as e:
                    _LOGGER.debug("Failed to read manufacturer: %s", e)
                
                # Add device-specific info reading
                await self._read_specific_info(client, info)
        except Exception as e:
            _LOGGER.debug("Failed to read device info: %s", e)
        
        self._info = info
        return info

    @abstractmethod
    async def _read_specific_info(self, client, info: Dict[str, Any]) -> None:
        """Read device-specific information."""
        pass


class ValveBasestationDevice(BasestationDevice):
    """Valve Index Basestation (V2) device."""

    @property
    def default_name(self) -> str:
        """Return the default name."""
        return "Valve Basestation"

    async def turn_on(self) -> None:
        """Turn on the device."""
        result = await self.async_ble_operation("write", V2_PWR_CHARACTERISTIC, value=V2_PWR_ON)
        if result:
            self._is_on = True

    async def turn_off(self) -> None:
        """Turn off the device."""
        result = await self.async_ble_operation("write", V2_PWR_CHARACTERISTIC, value=V2_PWR_SLEEP)
        if result:
            self._is_on = False

    async def update(self) -> None:
        """Update the device state."""
        value = await self.async_ble_operation("read", V2_PWR_CHARACTERISTIC)
        if value is not False:  # Check if operation was successful
            self._is_on = value != V2_PWR_SLEEP

    async def set_standby(self) -> None:
        """Set the device to standby mode."""
        result = await self.async_ble_operation("write", V2_PWR_CHARACTERISTIC, value=V2_PWR_STANDBY)
        if result:
            self._is_on = False

    async def identify(self) -> None:
        """Make the device blink its LED to identify it."""
        await self.async_ble_operation("write", V2_IDENTIFY_CHARACTERISTIC, value=b"\x00")

    async def _read_specific_info(self, client, info: Dict[str, Any]) -> None:
        """Read V2-specific information."""
        try:
            channel = await client.read_gatt_char(V2_CHANNEL_CHARACTERISTIC)
            info["channel"] = int.from_bytes(channel, byteorder='big')
        except Exception as e:
            _LOGGER.debug("Failed to read channel: %s", e)


class ViveBasestationDevice(BasestationDevice):
    """Vive Basestation (V1) device."""

    def __init__(self, hass: HomeAssistant, mac: str, name: Optional[str] = None, pair_id: Optional[int] = None):
        """Initialize the device."""
        super().__init__(hass, mac, name)
        self.pair_id = pair_id

    @property
    def default_name(self) -> str:
        """Return the default name."""
        return "Vive Basestation"

    async def turn_on(self) -> None:
        """Turn on the device."""
        if not self.pair_id:
            _LOGGER.error("Cannot turn on without pair ID")
            return

        try:
            # Create the command
            command = bytearray(20)
            command[0] = 0x12  # Command code
            command[1] = 0x00  # On state
            command[2] = 0x00
            command[3] = 0x00
            # Add pair ID in little endian
            pair_bytes = struct.pack("<I", self.pair_id)
            command[4:8] = pair_bytes
            
            result = await self.async_ble_operation("write", V1_PWR_CHARACTERISTIC, value=bytes(command))
            if result:
                self._is_on = True
        except Exception as e:
            _LOGGER.error("Failed to turn on V1 basestation: %s", e)
            self._available = False

    async def turn_off(self) -> None:
        """Turn off the device."""
        if not self.pair_id:
            _LOGGER.error("Cannot turn off without pair ID")
            return

        try:
            # Create the command
            command = bytearray(20)
            command[0] = 0x12  # Command code
            command[1] = 0x02  # Sleep state
            command[2] = 0x00
            command[3] = 0x01
            # Add pair ID in little endian
            pair_bytes = struct.pack("<I", self.pair_id)
            command[4:8] = pair_bytes
            
            result = await self.async_ble_operation("write", V1_PWR_CHARACTERISTIC, value=bytes(command))
            if result:
                self._is_on = False
        except Exception as e:
            _LOGGER.error("Failed to turn off V1 basestation: %s", e)
            self._available = False

    async def update(self) -> None:
        """Update the device state."""
        # V1 devices don't support reading the current state
        # We'll check if the device is still available
        try:
            ble_device = self.get_ble_device()
            if ble_device:
                self._available = True
            else:
                self._available = False
        except Exception:
            self._available = False

    async def _read_specific_info(self, client, info: Dict[str, Any]) -> None:
        """Read V1-specific information."""
        # Add pair ID to info
        if self.pair_id:
            info["pair_id"] = f"0x{self.pair_id:08X}"


def get_basestation_device(hass, mac, device_info) -> Optional[BasestationDevice]:
    """Create the appropriate device based on the device info."""
    name = device_info.get("name")
    
    # Check if we have a device type specified
    device_type = device_info.get("device_type")
    
    if device_type == "valve" or (name and name.startswith(V2_NAME_PREFIX)):
        return ValveBasestationDevice(hass, mac, name)
    elif device_type == "vive" or (name and name.startswith(V1_NAME_PREFIX)):
        # For V1 devices, we need the pair ID
        pair_id = device_info.get("pair_id")
        return ViveBasestationDevice(hass, mac, name, pair_id)
    
    # If we can't determine the type, default to Valve basestation
    _LOGGER.warning("Could not determine device type for %s, defaulting to Valve basestation", mac)
    return ValveBasestationDevice(hass, mac, name)