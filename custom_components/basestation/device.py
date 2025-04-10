"""Device classes for basestation integration."""
import logging
import asyncio
import struct
import time
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

from bleak import BleakClient, BleakError, BleakGATTCharacteristic
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
INFO_READ_RETRIES = 3  # Retries for device info reading

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
        self._last_power_state = None
        self._last_device_info_read = 0  # Timestamp of last device info read
        self._device_info_read_success = False  # Flag to track if we've ever successfully read device info

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
                                 retry: bool = True, value: bytes = None, 
                                 without_response: bool = False):
        """Execute a BLE operation with proper connection management."""
        result = None
        
        for attempt in range(MAX_RETRIES if retry else 1):
            try:
                async with CONNECTION_SEMAPHORE:
                    # Add delay to prevent overwhelming the BLE adapter
                    if attempt > 0:
                        await asyncio.sleep(CONNECTION_DELAY * (2 ** attempt))
                    else:
                        await asyncio.sleep(CONNECTION_DELAY * 0.5)  # Faster initial attempt
                    
                    # Get BLE device from registry
                    device = self.get_ble_device()
                    if not device:
                        _LOGGER.debug("Device %s not found in Bluetooth registry", self.mac)
                        continue

                    # Connect to device and execute operation
                    async with BleakClient(device, timeout=CONNECTION_TIMEOUT) as client:
                        if operation_type == "write":
                            await client.write_gatt_char(
                                characteristic_uuid, 
                                value, 
                                response=not without_response
                            )
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

    async def read_device_info(self, force=False) -> Dict[str, Any]:
        """Read device information characteristics.
        
        Args:
            force: If True, forces a read even if the cache time hasn't expired.
            
        Returns:
            Dictionary with device information.
        """
        # Only read device info at most once every 30 minutes unless forced
        current_time = time.time()
        if not force and self._device_info_read_success and (current_time - self._last_device_info_read < 1800):
            _LOGGER.debug("Using cached device info for %s", self.mac)
            return self._info
            
        # Try to read with multiple retries for reliability
        for attempt in range(INFO_READ_RETRIES):
            if attempt > 0:
                _LOGGER.debug("Retrying device info read (attempt %d/%d) for %s", 
                           attempt + 1, INFO_READ_RETRIES, self.mac)
                await asyncio.sleep(CONNECTION_DELAY * (2 ** attempt))
                
            info = {}
            read_success = False
            
            try:
                device = self.get_ble_device()
                if not device:
                    _LOGGER.debug("Device %s not found in Bluetooth registry for info read", self.mac)
                    continue
                    
                async with BleakClient(device, timeout=CONNECTION_TIMEOUT) as client:
                    # Try to read each characteristic, logging detailed errors
                    # Flag to track if we successfully read any info
                    any_read_successful = False
                    
                    try:
                        firmware = await client.read_gatt_char(FIRMWARE_CHARACTERISTIC)
                        if firmware:
                            info["firmware"] = firmware.decode('utf-8').strip()
                            _LOGGER.debug("Read firmware for %s: %s", self.mac, info["firmware"])
                            any_read_successful = True
                    except Exception as e:
                        _LOGGER.debug("Failed to read firmware for %s: %s", self.mac, e)

                    try:
                        model = await client.read_gatt_char(MODEL_CHARACTERISTIC)
                        if model:
                            info["model"] = model.decode('utf-8').strip()
                            _LOGGER.debug("Read model for %s: %s", self.mac, info["model"])
                            any_read_successful = True
                    except Exception as e:
                        _LOGGER.debug("Failed to read model for %s: %s", self.mac, e)

                    try:
                        hardware = await client.read_gatt_char(HARDWARE_CHARACTERISTIC)
                        if hardware:
                            info["hardware"] = hardware.decode('utf-8').strip()
                            _LOGGER.debug("Read hardware for %s: %s", self.mac, info["hardware"])
                            any_read_successful = True
                    except Exception as e:
                        _LOGGER.debug("Failed to read hardware for %s: %s", self.mac, e)

                    try:
                        manufacturer = await client.read_gatt_char(MANUFACTURER_CHARACTERISTIC)
                        if manufacturer:
                            info["manufacturer"] = manufacturer.decode('utf-8').strip()
                            _LOGGER.debug("Read manufacturer for %s: %s", self.mac, info["manufacturer"])
                            any_read_successful = True
                    except Exception as e:
                        _LOGGER.debug("Failed to read manufacturer for %s: %s", self.mac, e)
                    
                    # Add device-specific info reading
                    specific_info_success = await self._read_specific_info(client, info)
                    
                    # Set read_success flag if we read any info
                    read_success = any_read_successful or specific_info_success
                    
            except Exception as e:
                _LOGGER.debug("Failed to connect for device info read for %s: %s", self.mac, e)
            
            # Only update stored info and timestamp if we had a successful read
            if read_success:
                # Preserve existing info if new read doesn't provide it
                for key, value in self._info.items():
                    if key not in info:
                        info[key] = value
                        
                self._info = info
                self._available = True
                self._last_device_info_read = current_time
                self._device_info_read_success = True
                
                _LOGGER.info("Successfully read device info for %s, fields: %s", 
                          self.mac, ", ".join(info.keys()))
                
                return info
        
        # If all attempts failed, log a warning
        if not self._device_info_read_success:
            _LOGGER.warning("Failed to read any device info for %s after %d attempts", 
                         self.mac, INFO_READ_RETRIES)
        
        return self._info

    @abstractmethod
    async def _read_specific_info(self, client, info: Dict[str, Any]) -> bool:
        """Read device-specific information.
        
        Returns:
            True if any information was successfully read, False otherwise.
        """
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
            self._last_power_state = 0x0b  # Set to "on" state

    async def turn_off(self) -> None:
        """Turn off the device."""
        result = await self.async_ble_operation("write", V2_PWR_CHARACTERISTIC, value=V2_PWR_SLEEP)
        if result:
            self._is_on = False
            self._last_power_state = 0x00  # Set to "sleep" state

    async def update(self) -> None:
        """Update the device state."""
        value = await self.async_ble_operation("read", V2_PWR_CHARACTERISTIC)
        if value is not False and len(value) > 0:  # Check if operation was successful
            self._last_power_state = value[0]
            # Changed to consider the device "on" if not in sleep mode (0x00)
            # This means both normal operation (0x0b) and standby (0x02) will show as "on"
            self._is_on = value[0] != 0x00

    async def get_raw_power_state(self) -> Optional[int]:
        """Get the raw power state value."""
        # If we have a cached value, return it
        if self._last_power_state is not None:
            return self._last_power_state
            
        # Otherwise try to read it
        value = await self.async_ble_operation("read", V2_PWR_CHARACTERISTIC)
        if value is not False and len(value) > 0:  # Check if operation was successful
            self._last_power_state = value[0]
            return value[0]
        return None

    async def set_standby(self) -> None:
        """Set the device to standby mode."""
        result = await self.async_ble_operation("write", V2_PWR_CHARACTERISTIC, value=V2_PWR_STANDBY)
        if result:
            # Changed from False to True - standby mode should show the main switch as "on"
            self._is_on = True
            self._last_power_state = 0x02  # Set to "standby" state

    async def identify(self) -> None:
        """Make the device blink its LED to identify it."""
        # Try different approaches to trigger the identify function
        
        # Method 1: Direct connection with writeWithoutResponse
        try:
            device = self.get_ble_device()
            if not device:
                _LOGGER.warning("Device %s not found in Bluetooth registry", self.mac)
                return

            _LOGGER.debug("Sending identify command to %s using direct method", self.mac)
            async with BleakClient(device, timeout=CONNECTION_TIMEOUT) as client:
                # Always use writeWithoutResponse for identify characteristic
                await client.write_gatt_char(
                    V2_IDENTIFY_CHARACTERISTIC, 
                    b"\x00", 
                    response=False  # writeWithoutResponse
                )
                _LOGGER.info("Identify command sent to %s", self.mac)
                self._available = True
                return
                
        except Exception as e:
            _LOGGER.warning("Failed direct identify method: %s", str(e))
        
        # Method 2: Use our standard BLE operation with without_response=True
        try:
            _LOGGER.debug("Trying identify with standard BLE operation")
            result = await self.async_ble_operation(
                "write", 
                V2_IDENTIFY_CHARACTERISTIC, 
                value=b"\x00", 
                without_response=True
            )
            if result:
                _LOGGER.info("Identify command sent successfully with standard method")
                return
        except Exception as e:
            _LOGGER.warning("Failed standard identify method: %s", str(e))
            
        # Method 3: Last resort - try both 0x00 and 0x01 values
        try:
            _LOGGER.debug("Trying alternate identify value")
            await self.async_ble_operation(
                "write", 
                V2_IDENTIFY_CHARACTERISTIC, 
                value=b"\x01", 
                without_response=True
            )
            _LOGGER.info("Alternate identify command sent")
        except Exception as e:
            _LOGGER.error("All identify methods failed: %s", str(e))

    async def _read_specific_info(self, client, info: Dict[str, Any]) -> bool:
        """Read V2-specific information."""
        try:
            channel = await client.read_gatt_char(V2_CHANNEL_CHARACTERISTIC)
            if channel:
                info["channel"] = int.from_bytes(channel, byteorder='big')
                _LOGGER.debug("Read channel: %s", info["channel"])
                return True
        except Exception as e:
            _LOGGER.debug("Failed to read channel: %s", e)
        
        return False


class ViveBasestationDevice(BasestationDevice):
    """Vive Basestation (V1) device."""

    def __init__(self, hass: HomeAssistant, mac: str, name: Optional[str] = None, pair_id: Optional[int] = None):
        """Initialize the device."""
        super().__init__(hass, mac, name)
        self.pair_id = pair_id
        # Store pair_id in the info dictionary
        if pair_id is not None:
            self._info["pair_id"] = f"0x{pair_id:08X}"

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

    async def _read_specific_info(self, client, info: Dict[str, Any]) -> bool:
        """Read V1-specific information."""
        # Add pair ID to info if available
        if self.pair_id:
            info["pair_id"] = f"0x{self.pair_id:08X}"
            return True
        return False


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