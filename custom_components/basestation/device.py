"""
Device classes for basestation integration.

This module contains the core device classes for managing VR Base Stations.
It has been updated to use bleak-retry-connector instead of direct BleakClient
usage to eliminate Home Assistant warning spam and provide more reliable connections.

Key Changes in v2.0.2:
- Added proper connection state tracking to prevent multiple simultaneous connections
- Added cleanup methods for proper resource management
- Improved error recovery with exponential backoff
- Added connection cooldown to prevent overwhelming base stations
- Fixed resource exhaustion issues that caused devices to become unavailable

Key Changes in v2.0.3 (Polling Optimization):
- Power State sensor is now the single source of truth for device state
- Switches and buttons read from cached state instead of making separate BLE requests
- Device maintains _last_power_state with timestamp for state freshness checks
- Dramatically reduced BLE connection overhead from ~3 requests per update to just 1
"""

import asyncio
import logging
import struct
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from attr import dataclass
from bleak.exc import BleakError

# Import the recommended bleak-retry-connector for reliable BLE connections
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
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
    STANDBY_STATE_VALUE,
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

# Limit concurrent connections to prevent overwhelming the Bluetooth adapter
CONNECTION_SEMAPHORE = asyncio.Semaphore(2)
CONNECTION_DELAY = 0.5  # Delay between connections in seconds
MAX_RETRIES = 2  # Maximum connection retry attempts
INFO_READ_RETRIES = 3  # Retries for device info reading

# Connection cooldown to prevent rapid reconnection attempts
CONNECTION_COOLDOWN = 5.0  # Seconds to wait after failed connection before retry
MAX_CONSECUTIVE_FAILURES = 5  # Max failures before extended cooldown
EXTENDED_COOLDOWN = 30.0  # Extended cooldown for persistent failures
MIN_FAILURES_FOR_UNAVAILABLE = 3  # Mark device unavailable after this many consecutive failures

# State freshness for cached state
STATE_FRESHNESS_THRESHOLD = 10.0  # Seconds - consider state stale after this

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
        self.connection_timeout = connection_timeout
        self._is_on = False
        self._available = False
        self._info: dict[BaseStationDeviceInfoKey, str] = {}
        self._retry_count = 0
        self._last_power_state: int | None = None
        self._last_power_state_update = 0.0
        self._last_device_info_read = 0.0
        self._device_info_read_success = False

        # Connection state tracking
        self._is_connecting = False
        self._last_connection_attempt = 0.0
        self._consecutive_failures = 0
        self._last_successful_connection = 0.0

        # Current client reference (for cleanup)
        self._current_client: BleakClientWithServiceCache | None = None
        self._client_lock = asyncio.Lock()

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
    def last_power_state(self) -> int | None:
        """Return the last known power state value."""
        return self._last_power_state

    @property
    def has_fresh_state(self) -> bool:
        """Return True if we have a recent power state."""
        if self._last_power_state is None:
            return False

        age = time.time() - self._last_power_state_update
        return age < STATE_FRESHNESS_THRESHOLD

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

    async def cleanup(self) -> None:
        """Clean up resources when device is being removed."""
        async with self._client_lock:
            if self._current_client and self._current_client.is_connected:
                try:
                    await self._current_client.disconnect()
                    _LOGGER.debug("Disconnected client for %s during cleanup", self.mac)
                except Exception as e:
                    _LOGGER.debug("Error disconnecting client during cleanup: %s", e)
                finally:
                    self._current_client = None

        self._is_connecting = False
        self._available = False

    def _should_attempt_connection(self) -> bool:
        """Determine if we should attempt a connection based on recent failures."""
        current_time = time.time()

        # If we're already connecting, don't try again
        if self._is_connecting:
            return False

        # Calculate required cooldown based on consecutive failures
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            required_cooldown = EXTENDED_COOLDOWN
        elif self._consecutive_failures > 0:
            # Exponential backoff: 5s, 10s, 20s, etc.
            required_cooldown = CONNECTION_COOLDOWN * (2 ** (self._consecutive_failures - 1))
        else:
            required_cooldown = 0

        # Check if enough time has passed since last attempt
        time_since_last_attempt = current_time - self._last_connection_attempt
        return time_since_last_attempt >= required_cooldown

    def _record_connection_success(self) -> None:
        """Record a successful connection."""
        self._consecutive_failures = 0
        self._retry_count = 0
        self._available = True
        self._last_successful_connection = time.time()

    def _record_connection_failure(self) -> None:
        """Record a failed connection attempt."""
        self._consecutive_failures += 1
        self._retry_count += 1

        # Mark as unavailable after multiple failures
        if self._consecutive_failures >= MIN_FAILURES_FOR_UNAVAILABLE:
            self._available = False

    def _update_power_state(self, state: int) -> None:
        """
        Update the cached power state.

        This method is called when we successfully read the power state from the device.
        It updates both the state value and the timestamp.
        """
        self._last_power_state = state
        self._last_power_state_update = time.time()

        # Update the is_on flag based on state
        # Device is considered "on" if not in sleep mode (0x00)
        self._is_on = state != 0x00

    @overload
    async def async_ble_operation(self, op: BLEOperationRead) -> bytearray | Literal[False]: ...
    @overload
    async def async_ble_operation(self, op: BLEOperationWrite) -> bool: ...
    async def async_ble_operation(self, op: BLEOperationRead | BLEOperationWrite) -> bool | bytearray:
        """
        Execute a BLE operation with proper connection management.

        This method includes:
        - Connection state tracking to prevent multiple simultaneous connections
        - Exponential backoff for failed connections
        - Proper cleanup of connections
        - Connection cooldown to prevent overwhelming devices
        """
        # Check if we should attempt connection
        if not self._should_attempt_connection():
            _LOGGER.debug(
                "Skipping connection attempt for %s (consecutive failures: %d, cooldown active)",
                self.mac,
                self._consecutive_failures,
            )
            return False

        result: bool | bytearray
        self._last_connection_attempt = time.time()

        # Mark that we're connecting
        async with self._client_lock:
            if self._is_connecting:
                _LOGGER.debug("Already connecting to %s, skipping", self.mac)
                return False
            self._is_connecting = True

        try:
            for attempt in range(MAX_RETRIES if op.retry else 1):
                try:
                    async with CONNECTION_SEMAPHORE:
                        await connect_delay(attempt)

                        # Get BLE device from registry
                        device = self.get_ble_device()
                        if not device:
                            _LOGGER.debug("Device %s not found in Bluetooth registry", self.mac)
                            continue

                        # Establish connection using the recommended approach
                        client = await establish_connection(
                            BleakClientWithServiceCache,
                            device,
                            device.name or device.address,
                            disconnected_callback=self._handle_disconnect,
                            max_attempts=1,
                            use_services_cache=True,
                            ble_device_callback=lambda: self.get_ble_device(),
                        )

                        async with client:
                            # Store client reference for cleanup
                            async with self._client_lock:
                                self._current_client = client

                            if isinstance(op, BLEOperationRead):
                                result = await client.read_gatt_char(op.characteristic_uuid)
                            else:
                                await client.write_gatt_char(
                                    op.characteristic_uuid,
                                    op.value,
                                    response=not op.without_response,
                                )
                                result = True

                            # Record success
                            self._record_connection_success()
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

                # Small delay between retries
                if attempt < (MAX_RETRIES if op.retry else 1) - 1:
                    await asyncio.sleep(CONNECTION_DELAY)

            # All attempts failed
            self._record_connection_failure()

            # Log warning for persistent failures
            if self._consecutive_failures == MAX_CONSECUTIVE_FAILURES:
                _LOGGER.warning(
                    "Device %s has failed %d consecutive connection attempts. "
                    "Entering extended cooldown mode. Check if device is powered on and in range.",
                    self.mac,
                    self._consecutive_failures,
                )

            return False

        finally:
            # Always clear connecting flag and client reference
            async with self._client_lock:
                self._is_connecting = False
                self._current_client = None

    def _handle_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        """Handle disconnection callback from BLE client."""
        _LOGGER.debug("Device %s disconnected", self.mac)
        # Don't mark as unavailable on disconnect - it might just be between operations

    async def _read_standard_characteristics(
        self, client: BleakClientWithServiceCache, info: dict[BaseStationDeviceInfoKey, str]
    ) -> bool:
        """
        Read standard device info characteristics.

        Returns:
            True if any characteristic was successfully read, False otherwise.

        """
        any_read_successful = False

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

        return any_read_successful

    async def _attempt_device_info_read(self) -> dict[BaseStationDeviceInfoKey, str] | None:
        """
        Attempt a single device info read operation.

        Returns:
            Dictionary of device info if successful, None otherwise.

        """
        device = self.get_ble_device()
        if not device:
            _LOGGER.debug(
                "Device %s not found in Bluetooth registry for info read",
                self.mac,
            )
            return None

        info: dict[BaseStationDeviceInfoKey, str] = {}

        try:
            # Use establish_connection for reliable connection
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                device.name or device.address,
                disconnected_callback=self._handle_disconnect,
                max_attempts=1,
                use_services_cache=True,
                ble_device_callback=lambda: self.get_ble_device(),
            )

            async with client:
                # Store client reference
                async with self._client_lock:
                    self._current_client = client

                # Read standard characteristics
                standard_read_successful = await self._read_standard_characteristics(client, info)

                # Add device-specific info reading
                specific_info_success = await self._read_specific_info(client, info)

                # Return info if any read was successful
                if standard_read_successful or specific_info_success:
                    return info

        except Exception as e:
            _LOGGER.debug("Failed to connect for device info read for %s: %s", self.mac, e)

        finally:
            async with self._client_lock:
                self._is_connecting = False
                self._current_client = None

        return None

    async def read_device_info(self, /, *, force: bool = False) -> dict[BaseStationDeviceInfoKey, str]:
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

        # Check if we should attempt connection
        if not self._should_attempt_connection():
            _LOGGER.debug("Skipping device info read for %s due to cooldown", self.mac)
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

            self._last_connection_attempt = time.time()

            # Mark that we're connecting
            async with self._client_lock:
                if self._is_connecting:
                    _LOGGER.debug("Already connecting to %s for info read, skipping", self.mac)
                    return self._info
                self._is_connecting = True

            # Attempt to read device info
            info = await self._attempt_device_info_read()

            if info:
                # Success! Update stored info and return
                self._info |= info
                self._record_connection_success()
                self._last_device_info_read = current_time
                self._device_info_read_success = True

                _LOGGER.info(
                    "Successfully read device info for %s, fields: %s",
                    self.mac,
                    ", ".join(info.keys()),
                )
                return info

            # Failed this attempt, record it
            self._record_connection_failure()

        # All attempts failed
        if not self._device_info_read_success:
            _LOGGER.warning(
                "Failed to read any device info for %s after %d attempts",
                self.mac,
                INFO_READ_RETRIES,
            )

        return self._info

    @abstractmethod
    async def _read_specific_info(
        self, client: BleakClientWithServiceCache, info: dict[BaseStationDeviceInfoKey, Any]
    ) -> bool:
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

    @property
    def is_in_standby(self) -> bool:
        """Return True if device is in standby mode (0x02)."""
        return self._last_power_state == STANDBY_STATE_VALUE

    async def turn_on(self) -> None:
        """Turn on the device."""
        result = await self.async_ble_operation(BLEOperationWrite(V2_PWR_CHARACTERISTIC, V2_PWR_ON))
        if result:
            self._update_power_state(0x0B)  # On state

    async def turn_off(self) -> None:
        """Turn off the device."""
        result = await self.async_ble_operation(BLEOperationWrite(V2_PWR_CHARACTERISTIC, V2_PWR_SLEEP))
        if result:
            self._update_power_state(0x00)  # Sleep state

    async def update(self) -> None:
        """Update the device state."""
        value = await self.async_ble_operation(BLEOperationRead(V2_PWR_CHARACTERISTIC))
        if value and len(value) > 0:
            self._update_power_state(value[0])

    async def get_raw_power_state(self) -> int | None:
        """Get the raw power state value."""
        # If we have a recent cached value, return it
        if self._last_power_state is not None:
            return self._last_power_state

        # Otherwise try to read it
        value = await self.async_ble_operation(BLEOperationRead(V2_PWR_CHARACTERISTIC))
        if value is not False and len(value) > 0:
            self._update_power_state(value[0])
            return value[0]
        return None

    async def set_standby(self) -> None:
        """Set the device to standby mode."""
        result = await self.async_ble_operation(BLEOperationWrite(V2_PWR_CHARACTERISTIC, V2_PWR_STANDBY))
        if result:
            self._update_power_state(0x02)  # Standby state

    async def identify(self) -> None:
        """Make the device blink its LED to identify it."""
        try:
            result = await self.async_ble_operation(
                BLEOperationWrite(V2_IDENTIFY_CHARACTERISTIC, b"\x00", without_response=True),
            )
            if result:
                _LOGGER.info("Identify command sent successfully to %s", self.mac)
        except Exception:
            _LOGGER.exception("Failed to send identify command to %s", self.mac)

    async def _read_specific_info(
        self, client: BleakClientWithServiceCache, info: dict[BaseStationDeviceInfoKey, Any]
    ) -> bool:
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
            command = bytearray(20)
            command[0] = 0x12
            command[1] = 0x00
            command[2] = 0x00
            command[3] = 0x00
            command[4:8] = struct.pack("<I", self.pair_id)

            result = await self.async_ble_operation(
                BLEOperationWrite(V1_PWR_CHARACTERISTIC, bytes(command)),
            )
            if result:
                self._is_on = True
        except Exception:
            _LOGGER.exception("Failed to turn on V1 basestation")

    async def turn_off(self) -> None:
        """Turn off the device."""
        if not self.pair_id:
            _LOGGER.error("Cannot turn off without pair ID")
            return

        try:
            command = bytearray(20)
            command[0] = 0x12
            command[1] = 0x02
            command[2] = 0x00
            command[3] = 0x01
            command[4:8] = struct.pack("<I", self.pair_id)

            result = await self.async_ble_operation(
                BLEOperationWrite(V1_PWR_CHARACTERISTIC, bytes(command)),
            )
            if result:
                self._is_on = False
        except Exception:
            _LOGGER.exception("Failed to turn off V1 basestation")

    async def update(self) -> None:
        """Update the device state."""
        # V1 devices don't support reading the current state
        # Just check if device is discoverable
        try:
            ble_device = self.get_ble_device()
            self._available = ble_device is not None
        except Exception:
            self._available = False

    async def _read_specific_info(
        self, _client: BleakClientWithServiceCache, info: dict[BaseStationDeviceInfoKey, Any]
    ) -> bool:
        """Read V1-specific information."""
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
        return ViveBasestationDevice(hass, mac, name, pair_id, connection_timeout=connection_timeout)

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

    await asyncio.sleep(CONNECTION_DELAY * 0.5)
