"""Device classes for basestation integration."""

import asyncio
import logging
import struct
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from attr import dataclass
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
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

_LOGGER = logging.getLogger(__name__)

# Constants
CONNECTION_DELAY = 0.5
MAX_RETRIES = 2
INFO_READ_RETRIES = 3
CONNECTION_COOLDOWN = 5.0
MAX_CONSECUTIVE_FAILURES = 5
EXTENDED_COOLDOWN = 30.0
MIN_FAILURES_FOR_UNAVAILABLE = 3
STATE_FRESHNESS_THRESHOLD = 10.0

type BaseStationDeviceInfoKey = Literal["firmware", "model", "hardware", "manufacturer", "channel", "pair_id"]


@dataclass(repr=False)
class BLEOperationRead:
    """BLE read operation."""

    characteristic_uuid: str
    retry: bool = True


@dataclass(repr=False)
class BLEOperationWrite:
    """BLE write operation."""

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

        self._is_connecting = False
        self._last_connection_attempt = 0.0
        self._consecutive_failures = 0
        self._last_successful_connection = 0.0

        self._current_client: BleakClientWithServiceCache | None = None
        self._client_lock = asyncio.Lock()

    @property
    def device_name(self) -> str:
        """Return the name of the device."""
        return self.custom_name or self.default_name

    @property
    def is_on(self) -> bool:
        """Return if device is on or not."""
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

    def get_ble_device(self) -> BLEDevice | None:
        """Get the BLE device from the address."""
        return bluetooth.async_ble_device_from_address(self.hass, self.mac)

    async def cleanup(self) -> None:
        """Clean up resources when device is being removed."""
        async with self._client_lock:
            if self._current_client and self._current_client.is_connected:
                try:
                    await self._current_client.disconnect()
                except Exception as e:
                    _LOGGER.debug("Error disconnecting client during cleanup: %s", e)
                finally:
                    self._current_client = None

        self._is_connecting = False
        self._available = False

    def _should_attempt_connection(self) -> bool:
        current_time = time.time()
        if self._is_connecting:
            return False

        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            required_cooldown = EXTENDED_COOLDOWN
        elif self._consecutive_failures > 0:
            required_cooldown = CONNECTION_COOLDOWN * (2 ** (self._consecutive_failures - 1))
        else:
            required_cooldown = 0

        time_since_last_attempt = current_time - self._last_connection_attempt
        return time_since_last_attempt >= required_cooldown

    def _record_connection_success(self) -> None:
        self._consecutive_failures = 0
        self._retry_count = 0
        self._available = True
        self._last_successful_connection = time.time()

    def _record_connection_failure(self) -> None:
        self._consecutive_failures += 1
        self._retry_count += 1
        if self._consecutive_failures >= MIN_FAILURES_FOR_UNAVAILABLE:
            self._available = False

    def _update_power_state(self, state: int) -> None:
        self._last_power_state = state
        self._last_power_state_update = time.time()
        self._is_on = state != 0x00

    @overload
    async def async_ble_operation(self, op: BLEOperationRead) -> bytearray | Literal[False]: ...
    @overload
    async def async_ble_operation(self, op: BLEOperationWrite) -> bool: ...
    async def async_ble_operation(self, op: BLEOperationRead | BLEOperationWrite) -> bool | bytearray:
        """Execute a BLE operation with proper connection management."""
        if not self._should_attempt_connection():
            return False

        result: bool | bytearray
        self._last_connection_attempt = time.time()

        async with self._client_lock:
            if self._is_connecting:
                return False
            self._is_connecting = True

        try:
            for attempt in range(MAX_RETRIES if op.retry else 1):
                try:
                    await connect_delay(attempt)
                    device = self.get_ble_device()
                    if not device:
                        continue

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

                        self._record_connection_success()
                        return result

                except BleakError as err:
                    _LOGGER.debug("BLE error on %s: %s", self.mac, str(err))
                except Exception as ex:
                    _LOGGER.debug("Failed to execute op on %s: %s", self.mac, str(ex))

                if attempt < (MAX_RETRIES if op.retry else 1) - 1:
                    await asyncio.sleep(CONNECTION_DELAY)

            self._record_connection_failure()
            if self._consecutive_failures == MAX_CONSECUTIVE_FAILURES:
                _LOGGER.warning("Device %s connection failed repeatedly.", self.mac)
            return False

        finally:
            async with self._client_lock:
                self._is_connecting = False
                self._current_client = None

    def _handle_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.debug("Device %s disconnected", self.mac)

    async def _read_standard_characteristics(
        self, client: BleakClientWithServiceCache, info: dict[BaseStationDeviceInfoKey, str]
    ) -> bool:
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
                    any_read_successful = True
            except Exception:
                _LOGGER.debug("Failed to read characteristic %s", key)
        return any_read_successful

    async def _attempt_device_info_read(self) -> dict[BaseStationDeviceInfoKey, str] | None:
        device = self.get_ble_device()
        if not device:
            return None

        info: dict[BaseStationDeviceInfoKey, str] = {}
        try:
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
                async with self._client_lock:
                    self._current_client = client

                std_success = await self._read_standard_characteristics(client, info)
                spec_success = await self._read_specific_info(client, info)

                if std_success or spec_success:
                    return info
        except Exception as err:
            _LOGGER.debug("Failed to read device info: %s", err)
        finally:
            async with self._client_lock:
                self._is_connecting = False
                self._current_client = None
        return None

    async def read_device_info(self, /, *, force: bool = False) -> dict[BaseStationDeviceInfoKey, str]:
        """Read device information characteristics."""
        current_time = time.time()
        if (
            not force
            and self._device_info_read_success
            and (current_time - self._last_device_info_read < INFO_SENSOR_SCAN_INTERVAL)
        ):
            return self._info

        if not self._should_attempt_connection():
            return self._info

        for attempt in range(INFO_READ_RETRIES):
            if attempt > 0:
                await asyncio.sleep(CONNECTION_DELAY * (2**attempt))

            self._last_connection_attempt = time.time()
            async with self._client_lock:
                if self._is_connecting:
                    return self._info
                self._is_connecting = True

            info = await self._attempt_device_info_read()
            if info:
                self._info |= info
                self._record_connection_success()
                self._last_device_info_read = current_time
                self._device_info_read_success = True
                return info

            self._record_connection_failure()

        return self._info

    @abstractmethod
    async def _read_specific_info(
        self, client: BleakClientWithServiceCache, info: dict[BaseStationDeviceInfoKey, Any]
    ) -> bool:
        pass


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
            self._update_power_state(0x0B)

    async def turn_off(self) -> None:
        """Turn off the device."""
        result = await self.async_ble_operation(BLEOperationWrite(V2_PWR_CHARACTERISTIC, V2_PWR_SLEEP))
        if result:
            self._update_power_state(0x00)

    async def update(self) -> None:
        """Update the device state."""
        value = await self.async_ble_operation(BLEOperationRead(V2_PWR_CHARACTERISTIC))
        if value and len(value) > 0:
            self._update_power_state(value[0])

    async def get_raw_power_state(self) -> int | None:
        """Get the raw power state value."""
        if self._last_power_state is not None:
            return self._last_power_state
        value = await self.async_ble_operation(BLEOperationRead(V2_PWR_CHARACTERISTIC))
        if value is not False and len(value) > 0:
            self._update_power_state(value[0])
            return value[0]
        return None

    async def set_standby(self) -> None:
        """Set the device to standby mode."""
        result = await self.async_ble_operation(BLEOperationWrite(V2_PWR_CHARACTERISTIC, V2_PWR_STANDBY))
        if result:
            self._update_power_state(0x02)

    async def identify(self) -> None:
        """Make the device blink its LED to identify it."""
        await self.async_ble_operation(BLEOperationWrite(V2_IDENTIFY_CHARACTERISTIC, b"\x00", without_response=True))

    async def _read_specific_info(
        self, client: BleakClientWithServiceCache, info: dict[BaseStationDeviceInfoKey, Any]
    ) -> bool:
        try:
            channel = await client.read_gatt_char(V2_CHANNEL_CHARACTERISTIC)
            if channel:
                info["channel"] = int.from_bytes(channel, byteorder="big")
                return True
        except Exception:
            _LOGGER.debug("Failed to read channel")
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
            return
        try:
            command = bytearray(20)
            command[0:4] = b"\x12\x00\x00\x00"
            command[4:8] = struct.pack("<I", self.pair_id)
            if await self.async_ble_operation(BLEOperationWrite(V1_PWR_CHARACTERISTIC, bytes(command))):
                self._is_on = True
        except Exception:
            _LOGGER.debug("Failed to turn on V1 basestation")

    async def turn_off(self) -> None:
        """Turn off the device."""
        if not self.pair_id:
            return
        try:
            command = bytearray(20)
            command[0:4] = b"\x12\x02\x00\x01"
            command[4:8] = struct.pack("<I", self.pair_id)
            if await self.async_ble_operation(BLEOperationWrite(V1_PWR_CHARACTERISTIC, bytes(command))):
                self._is_on = False
        except Exception:
            _LOGGER.debug("Failed to turn off V1 basestation")

    async def update(self) -> None:
        """Update the device state."""
        try:
            ble_device = self.get_ble_device()
            self._available = ble_device is not None
        except Exception:
            self._available = False

    async def _read_specific_info(
        self, _client: BleakClientWithServiceCache, info: dict[BaseStationDeviceInfoKey, Any]
    ) -> bool:
        if self.pair_id:
            info["pair_id"] = f"0x{self.pair_id:08X}"
            return True
        return False


def get_basestation_device(hass: HomeAssistant, mac: str, /, **kwargs: Any) -> BasestationDevice:
    """Create the appropriate device based on the device info."""
    name = kwargs.get("name")
    device_type = kwargs.get("device_type")
    pair_id = kwargs.get("pair_id")
    connection_timeout = kwargs.get("connection_timeout", DEFAULT_CONNECTION_TIMEOUT)

    if device_type == DEVICE_TYPE_V2 or (name and name.startswith(V2_NAME_PREFIX)):
        return ValveBasestationDevice(hass, mac, name, connection_timeout=connection_timeout)
    if device_type == DEVICE_TYPE_V1 or (name and name.startswith(V1_NAME_PREFIX)):
        return ViveBasestationDevice(hass, mac, name, pair_id, connection_timeout=connection_timeout)

    return ValveBasestationDevice(hass, mac, name, connection_timeout=connection_timeout)


async def connect_delay(attempt: int) -> None:
    """Delay based on prior connection attempts."""
    if attempt > 0:
        await asyncio.sleep(CONNECTION_DELAY * (2**attempt))
    await asyncio.sleep(CONNECTION_DELAY * 0.5)
