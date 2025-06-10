"""Device classes for basestation integration."""

import asyncio
import logging
import struct
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from attr import dataclass
from bleak import BleakClient
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_CONNECTION_TIMEOUT,
    DEVICE_TYPE_V1,
    DEVICE_TYPE_V2,
    FIRMWARE_CHARACTERISTIC,
    HARDWARE_CHARACTERISTIC,
    INFO_SENSOR_SCAN_INTERVAL,
    MANUFACTURER_CHARACTERISTIC,
    MODEL_CHARACTERISTIC,
    V1_NAME_PREFIX,
    V1_PWR_CHARACTERISTIC,
    V2_CHANNEL_CHARACTERISTIC,
    V2_IDENTIFY_CHARACTERISTIC,
    V2_NAME_PREFIX,
    V2_PWR_CHARACTERISTIC,
    V2_PWR_ON,
    V2_PWR_SLEEP,
    V2_PWR_STANDBY,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

# Limit concurrent connections
CONNECTION_SEMAPHORE = asyncio.Semaphore(2)
CONNECTION_DELAY = 0.5  # Delay between connections in seconds
MAX_RETRIES = 2  # Maximum connection retry attempts
INFO_READ_RETRIES = 3  # Retries for device info reading

type BaseStationDeviceInfoKey = Literal["firmware", "model", "hardware", "manufacturer", "channel", "pair_id"]


@dataclass(repr=False)
class BLEOperationRead:
    """BLE read operation"""

    characteristic_uuid: str
    retry: bool = True


@dataclass(repr=False)
class BLEOperationWrite:
    """BLE write operation"""

    characteristic_uuid: str
    value: bytes

    retry: bool = True
    without_response: bool = False


@dataclass
class DeviceConfig:
    """Configuration for creating a basestation device."""

    hass: HomeAssistant
    mac: str
    name: str | None = None
    device_type: Literal["valve", "vive"] | None = None
    pair_id: int | None = None
    connection_timeout: int = DEFAULT_CONNECTION_TIMEOUT


class BasestationDevice(ABC):
    """Base class for basestation devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac: str,
        name: str | None = None,
        connection_timeout: int = DEFAULT_CONNECTION_TIMEOUT,
    ) -> None:
        """Initialize the device."""
        self.hass = hass
        self.mac = mac
        self.custom_name = name
        self.connection_timeout = connection_timeout  # User-configurable timeout
        self._is_on = False
        self._available = False
        self._info: dict[BaseStationDeviceInfoKey, str] = {}
        self._retry_count = 0
        self._last_power_state: int | None = None
        self._last_device_info_read = 0.0  # Timestamp of last device info read
        self._device_info_read_success = False  # Flag to track if we've ever successfully read device info

    @property
    def device_name(self) -> str:
        """Return the name of the device."""
        return self.custom_name or self.default_name

    @property
    def is_on(self) -> bool:
        """
        Return if device is on or not.

        Currently, both running and standby are considered on
        """
        return self._is_on

    @property
    def available(self) -> bool:
        """Return if the device is available."""
        return self._available

    @property
    @abstractmethod
    def default_name(self) -> str:
        """Return the default name for this device type."""

    @abstractmethod
    async def turn_on(self) -> None:
        """Turn on the device."""

    @abstractmethod
    async def turn_off(self) -> None:
        """Turn off the device."""

    @abstractmethod
    async def update(self) -> None:
        """Update the device state."""

    def get_info(self, key: BaseStationDeviceInfoKey, default: Any | None = None) -> str | None:
        """Get device info by key."""
        return self._info.get(key, default)

    def get_ble_device(self) -> "BLEDevice | None":
        """Get the BLE device from the address."""
        return bluetooth.async_ble_device_from_address(self.hass, self.mac)

    @overload
    async def async_ble_operation(self, op: BLEOperationRead) -> bytearray | Literal[False]: ...
    @overload
    async def async_ble_operation(self, op: BLEOperationWrite) -> bool: ...
    async def async_ble_operation(self, op: BLEOperationRead | BLEOperationWrite) -> bool | bytearray:
        """Execute a BLE operation with proper connection management."""
        result: bool | bytearray

        for attempt in range(MAX_RETRIES if op.retry else 1):
            try:
                async with CONNECTION_SEMAPHORE:
                    await connect_delay(attempt)

                    # Get BLE device from registry
                    device = self.get_ble_device()
                    if not device:
                        _LOGGER.debug("Device %s not found in Bluetooth registry", self.mac)
                        continue

                    # Connect to device and execute operation using user-configured timeout
                    async with BleakClient(device, timeout=self.connection_timeout) as client:
                        if isinstance(op, BLEOperationRead):
                            result = await client.read_gatt_char(op.characteristic_uuid)
                        else:
                            await client.write_gatt_char(
                                op.characteristic_uuid,
                                op.value,
                                response=not op.without_response,
                            )
                            result = True

                        # Reset retry count on success
                        self._retry_count = 0
                        self._available = True
                        return result

            except BleakError as err:
                _LOGGER.debug(
                    "BLE error on basestation '%s' (attempt %d/%d): %s",
                    self.mac,
                    attempt + 1,
                    MAX_RETRIES if op.retry else 1,
                    str(err),
                )

            except Exception as ex:
                _LOGGER.debug(
                    "Failed to execute %s on basestation '%s': %s",
                    op,
                    self.mac,
                    str(ex),
                )

            # Increment retry count
            self._retry_count += 1

        # If we get here, all attempts failed
        self._available = False
        return False

    async def read_device_info(self, /, *, force: bool = False) -> dict[BaseStationDeviceInfoKey, str]:  # noqa: C901
        """
        Read device information characteristics.

        Args:
            force: If True, forces a read even if the cache time hasn't expired.

        Returns:
            Dictionary with device information.

        """
        # Only read device info at most once every 30 minutes unless forced
        current_time = time.time()
        if (
            not force
            and self._device_info_read_success
            and (current_time - self._last_device_info_read < INFO_SENSOR_SCAN_INTERVAL)
        ):
            _LOGGER.debug("Using cached device info for %s", self.mac)
            return self._info

        # Try to read with multiple retries for reliability
        for attempt in range(INFO_READ_RETRIES):
            if attempt > 0:
                _LOGGER.debug(
                    "Retrying device info read (attempt %d/%d) for %s",
                    attempt + 1,
                    INFO_READ_RETRIES,
                    self.mac,
                )
                await asyncio.sleep(CONNECTION_DELAY * (2**attempt))

            try:
                device = self.get_ble_device()
                if not device:
                    _LOGGER.debug(
                        "Device %s not found in Bluetooth registry for info read",
                        self.mac,
                    )
                    continue

                info: dict[BaseStationDeviceInfoKey, str] = {}
                # Use user-configured timeout for device info reading
                async with BleakClient(device, timeout=self.connection_timeout) as client:
                    # Try to read each characteristic, logging detailed errors
                    # Flag to track if we successfully read any info
                    any_read_successful: bool = False

                    for characteristic, key in cast(
                        "Iterable[tuple[str, BaseStationDeviceInfoKey]]",
                        (
                            (FIRMWARE_CHARACTERISTIC, "firmware"),
                            (MODEL_CHARACTERISTIC, "model"),
                            (HARDWARE_CHARACTERISTIC, "hardware"),
                            (MANUFACTURER_CHARACTERISTIC, "manufacturer"),
                        ),
                    ):
                        try:
                            if _value := await client.read_gatt_char(characteristic):
                                info[key] = _value.decode("utf-8").strip()
                                _LOGGER.debug("Read %s for %s: %s", key, self.mac, _value)
                                any_read_successful = True
                        except Exception as e:
                            _LOGGER.debug("Failed to read %s for %s: %s", key, self.mac, e)

                    # Add device-specific info reading
                    specific_info_success = await self._read_specific_info(client, info)

                    # Only update stored info and timestamp if we had a successful read
                    if any_read_successful or specific_info_success:
                        # Preserve existing info if new read doesn't provide it
                        self._info |= info

                        self._available = True
                        self._last_device_info_read = current_time
                        self._device_info_read_success = True

                        _LOGGER.info(
                            "Successfully read device info for %s, fields: %s",
                            self.mac,
                            ", ".join(info.keys()),
                        )

                        return info
            except Exception as e:
                _LOGGER.debug("Failed to connect for device info read for %s: %s", self.mac, e)

        # If all attempts failed, log a warning
        if not self._device_info_read_success:
            _LOGGER.warning(
                "Failed to read any device info for %s after %d attempts",
                self.mac,
                INFO_READ_RETRIES,
            )

        return self._info

    @abstractmethod
    async def _read_specific_info(self, client: BleakClient, info: dict[BaseStationDeviceInfoKey, Any]) -> bool:
        """
        Read device-specific information.

        Returns:
            True if any information was successfully read, False otherwise.

        """


class ValveBasestationDevice(BasestationDevice):
    """Valve Index Basestation (V2) device."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac: str,
        name: str | None = None,
        connection_timeout: int = DEFAULT_CONNECTION_TIMEOUT,
    ) -> None:
        """Initialize the Valve basestation device."""
        super().__init__(hass, mac, name, connection_timeout)

    @property
    def default_name(self) -> str:
        """Return the default name."""
        return "Valve Basestation"

    async def turn_on(self) -> None:
        """Turn on the device."""
        result = await self.async_ble_operation(BLEOperationWrite(V2_PWR_CHARACTERISTIC, V2_PWR_ON))
        if result:
            self._is_on = True
            self._last_power_state = 0x0B  # Set to "on" state

    async def turn_off(self) -> None:
        """Turn off the device."""
        result = await self.async_ble_operation(BLEOperationWrite(V2_PWR_CHARACTERISTIC, V2_PWR_SLEEP))
        if result:
            self._is_on = False
            self._last_power_state = 0x00  # Set to "sleep" state

    async def update(self) -> None:
        """Update the device state."""
        value = await self.async_ble_operation(BLEOperationRead(V2_PWR_CHARACTERISTIC))
        if value and len(value) > 0:  # Check if operation was successful
            self._last_power_state = value[0]
            # Changed to consider the device "on" if not in sleep mode (0x00)
            # This means both normal operation (0x0b) and standby (0x02) will show as "on"
            self._is_on = value[0] != 0x00

    async def get_raw_power_state(self) -> int | None:
        """Get the raw power state value."""
        # If we have a cached value, return it
        if self._last_power_state is not None:
            return self._last_power_state

        # Otherwise try to read it
        value = await self.async_ble_operation(BLEOperationRead(V2_PWR_CHARACTERISTIC))
        if value is not False and len(value) > 0:  # Check if operation was successful
            self._last_power_state = value[0]
            return value[0]
        return None

    async def set_standby(self) -> None:
        """Set the device to standby mode."""
        result = await self.async_ble_operation(BLEOperationWrite(V2_PWR_CHARACTERISTIC, V2_PWR_STANDBY))
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
            async with BleakClient(device, timeout=self.connection_timeout) as client:
                # Always use writeWithoutResponse for identify characteristic
                await client.write_gatt_char(
                    V2_IDENTIFY_CHARACTERISTIC,
                    b"\x00",
                    response=False,  # writeWithoutResponse
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
                BLEOperationWrite(V2_IDENTIFY_CHARACTERISTIC, b"\x00", without_response=True),
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
                BLEOperationWrite(V2_IDENTIFY_CHARACTERISTIC, b"\x01", without_response=True),
            )
            _LOGGER.info("Alternate identify command sent")
        except Exception:
            _LOGGER.exception("All identify methods failed")

    async def _read_specific_info(self, client: BleakClient, info: dict[BaseStationDeviceInfoKey, Any]) -> bool:
        """Read V2-specific information."""
        try:
            channel = await client.read_gatt_char(V2_CHANNEL_CHARACTERISTIC)
            if channel:
                info["channel"] = int.from_bytes(channel, byteorder="big")
                _LOGGER.debug("Read channel: %s", info["channel"])
                return True
        except Exception as e:
            _LOGGER.debug("Failed to read channel: %s", e)

        return False


class ViveBasestationDevice(BasestationDevice):
    """Vive Basestation (V1) device."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac: str,
        name: str | None = None,
        pair_id: int | None = None,
        connection_timeout: int = DEFAULT_CONNECTION_TIMEOUT,
    ) -> None:
        """Initialize the Vive basestation device."""
        super().__init__(hass, mac, name, connection_timeout)
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
            command[4:8] = struct.pack("<I", self.pair_id)

            result = await self.async_ble_operation(
                BLEOperationWrite(V1_PWR_CHARACTERISTIC, bytes(command)),
            )
            if result:
                self._is_on = True
        except Exception:
            _LOGGER.exception("Failed to turn on V1 basestation")
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
            command[4:8] = struct.pack("<I", self.pair_id)

            result = await self.async_ble_operation(
                BLEOperationWrite(V1_PWR_CHARACTERISTIC, bytes(command)),
            )
            if result:
                self._is_on = False
        except Exception:
            _LOGGER.exception("Failed to turn off V1 basestation")
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

    async def _read_specific_info(self, _client: BleakClient, info: dict[BaseStationDeviceInfoKey, Any]) -> bool:
        """Read V1-specific information."""
        # Add pair ID to info if available
        if self.pair_id:
            info["pair_id"] = f"0x{self.pair_id:08X}"
            return True
        return False


def get_basestation_device(
    hass: HomeAssistant,
    mac: str,
    /,
    **kwargs: Any,
) -> BasestationDevice:
    """Create the appropriate device based on the device info."""
    name = kwargs.get("name")
    device_type = kwargs.get("device_type")
    pair_id = kwargs.get("pair_id")
    connection_timeout = kwargs.get("connection_timeout", DEFAULT_CONNECTION_TIMEOUT)

    if device_type == DEVICE_TYPE_V2 or (name and name.startswith(V2_NAME_PREFIX)):
        return ValveBasestationDevice(hass, mac, name, connection_timeout=connection_timeout)
    if device_type == DEVICE_TYPE_V1 or (name and name.startswith(V1_NAME_PREFIX)):
        # For V1 devices, we need the pair ID
        return ViveBasestationDevice(hass, mac, name, pair_id, connection_timeout=connection_timeout)

    # If we can't determine the type, default to Valve basestation
    _LOGGER.warning("Could not determine device type for %s, defaulting to Valve basestation", mac)
    return ValveBasestationDevice(hass, mac, name, connection_timeout=connection_timeout)


def create_basestation_device_from_config(config: DeviceConfig) -> BasestationDevice:
    """Create a basestation device from a DeviceConfig object."""
    return get_basestation_device(
        config.hass,
        config.mac,
        name=config.name,
        device_type=config.device_type,
        pair_id=config.pair_id,
        connection_timeout=config.connection_timeout,
    )


async def connect_delay(attempt: int) -> None:
    """Delay based on prior connection attempts to not overwhelm the BLE adapter."""
    if attempt > 0:
        await asyncio.sleep(CONNECTION_DELAY * (2**attempt))

    # Faster initial attempt
    await asyncio.sleep(CONNECTION_DELAY * 0.5)
