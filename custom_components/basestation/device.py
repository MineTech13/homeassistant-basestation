"""Device classes for basestation integration."""

import asyncio
import contextlib
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
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from .const import (
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_INFO_SCAN_INTERVAL,
    DEVICE_TYPE_V1,
    DEVICE_TYPE_V2,
    DOMAIN,
    FIRMWARE_CHARACTERISTIC,
    HARDWARE_CHARACTERISTIC,
    MANUFACTURER_CHARACTERISTIC,
    MODEL_CHARACTERISTIC,
    V1_NAME_PREFIX,
    V1_PWR_CHARACTERISTIC,
    V2_CHANNEL_CHARACTERISTIC,
    V2_IDENTIFY_CHARACTERISTIC,
    V2_NAME_PREFIX,
    V2_PWR_CHARACTERISTIC,
    BasestationPowerState,
    V1Command,
    V2Command,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_LOGGER = logging.getLogger(__name__)

# Constants
CONNECTION_DELAY = 0.5
MAX_RETRIES = 2
INFO_READ_RETRIES = 3
STATE_FRESHNESS_THRESHOLD = 10.0

type BaseStationDeviceInfoKey = Literal["firmware", "model", "hardware", "manufacturer", "channel", "pair_id"]


@dataclass(repr=False)
class BLEOperationRead:
    """BLE read operation."""

    characteristic_uuid: str
    retry: bool = True
    keep_alive: bool = False


@dataclass(repr=False)
class BLEOperationWrite:
    """BLE write operation."""

    characteristic_uuid: str
    value: bytes
    retry: bool = True
    without_response: bool = False
    repeat_count: int = 1
    repeat_delay: float = 1.0
    keep_alive: bool = False


class BasestationDevice(ABC):
    """Base class for basestation devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac: str,
        name: str | None = None,
        connection_timeout: int = DEFAULT_CONNECTION_TIMEOUT,
        info_scan_interval: int = DEFAULT_INFO_SCAN_INTERVAL,
    ) -> None:
        """Initialize the device."""
        self.hass = hass
        self.mac = mac
        self.custom_name = name
        self.connection_timeout = connection_timeout
        self.info_scan_interval = info_scan_interval

        self._is_on = False
        self._available = False
        self._info: dict[BaseStationDeviceInfoKey, str] = {}
        self._retry_count = 0
        self._last_power_state: int | None = None
        self._last_power_state_update = 0.0
        self._last_device_info_read = 0.0
        self._device_info_read_success = False

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
    def cached_info(self) -> dict[BaseStationDeviceInfoKey, str]:
        """Return the cached device information."""
        return self._info

    @property
    def has_cached_info(self) -> bool:
        """Return True if device info has been successfully read."""
        return self._device_info_read_success

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info for the registry."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.mac)},
            connections={(CONNECTION_BLUETOOTH, self.mac)},
            name=self.device_name,
            manufacturer="Valve" if self.default_name == "Valve Basestation" else "HTC",
            model=self.default_name,
            serial_number=self.mac,
            sw_version=self.get_info("firmware"),
            hw_version=self.get_info("hardware"),
        )

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

    async def _disconnect_client(self) -> None:
        """Safely disconnect the current BLE client."""
        if self._current_client:
            with contextlib.suppress(Exception):
                if self._current_client.is_connected:
                    await self._current_client.disconnect()
            self._current_client = None

    async def cleanup(self) -> None:
        """Clean up resources when device is being removed."""
        client_to_disconnect = None
        has_lock = False

        try:
            async with asyncio.timeout(2.0):
                await self._client_lock.acquire()
                has_lock = True
        except TimeoutError:
            pass

        try:
            if self._current_client and self._current_client.is_connected:
                client_to_disconnect = self._current_client
            self._current_client = None
        finally:
            if has_lock:
                self._client_lock.release()

        if client_to_disconnect:
            try:
                async with asyncio.timeout(5.0):
                    await client_to_disconnect.disconnect()
            except (TimeoutError, Exception) as e:
                _LOGGER.debug("Error disconnecting client during cleanup: %s", e)

        self._available = False

    def _record_connection_success(self) -> None:
        self._consecutive_failures = 0
        self._retry_count = 0
        self._available = True
        self._last_successful_connection = time.time()

    def _record_connection_failure(self) -> None:
        self._consecutive_failures += 1
        self._retry_count += 1

    def _update_power_state(self, state: int) -> None:
        self._last_power_state = state
        self._last_power_state_update = time.time()
        self._is_on = state != 0x00

    async def _perform_ble_operation(
        self, client: BleakClientWithServiceCache, op: BLEOperationRead | BLEOperationWrite
    ) -> bool | bytearray | None:
        """Perform the read or write operation on the connected client."""
        if isinstance(op, BLEOperationRead):
            return await client.read_gatt_char(op.characteristic_uuid)

        for i in range(op.repeat_count):
            await client.write_gatt_char(
                op.characteristic_uuid,
                op.value,
                response=not op.without_response,
            )
            if i < op.repeat_count - 1:
                await asyncio.sleep(op.repeat_delay)
        return True

    async def _execute_single_ble_attempt(
        self, op: BLEOperationRead | BLEOperationWrite, attempt: int
    ) -> bool | bytearray | None:
        """Execute a single BLE connection and operation attempt. Returns None on failure."""
        if self._current_client and self._current_client.is_connected:
            try:
                _LOGGER.debug("Reusing existing BLE connection for %s", self.mac)
                result = await self._perform_ble_operation(self._current_client, op)
            except Exception as err:
                _LOGGER.debug("Failed to reuse connection for %s: %s", self.mac, err)
                await self._disconnect_client()
            else:
                if not op.keep_alive:
                    await self._disconnect_client()
                    await asyncio.sleep(0.5)
                return result

        return await self._establish_and_perform(op, attempt)

    async def _get_or_create_client(self, device: BLEDevice) -> tuple[BleakClientWithServiceCache, bool]:
        """Get existing or establish new BLE connection. Returns client and if it was reused."""
        if self._current_client and self._current_client.is_connected:
            _LOGGER.debug("Reusing existing BLE connection for %s", self.mac)
            return self._current_client, True

        async with asyncio.timeout(self.connection_timeout):
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                device.name or device.address,
                disconnected_callback=self._handle_disconnect,
                max_attempts=1,
                use_services_cache=True,
            )
        self._current_client = client
        return client, False

    async def _establish_and_perform(
        self, op: BLEOperationRead | BLEOperationWrite, attempt: int
    ) -> bool | bytearray | None:
        """Establish a new BLE connection and perform the operation."""
        try:
            await connect_delay(attempt)
            device = self.get_ble_device()
            if not device:
                return None

            client, _ = await self._get_or_create_client(device)
            result = await self._perform_ble_operation(client, op)

        except BleakError as err:
            _LOGGER.debug("BLE error on %s: %s", self.mac, str(err))
        except TimeoutError as err:
            _LOGGER.debug("Timeout executing BLE op on %s: %s", self.mac, str(err))
        except Exception:
            _LOGGER.exception("Unexpected error on %s", self.mac)
        else:
            self._record_connection_success()
            if not op.keep_alive:
                await self._disconnect_client()
                await asyncio.sleep(0.5)
            return result
        finally:
            if not op.keep_alive:
                await self._disconnect_client()

        return None

    @overload
    async def async_ble_operation(self, op: BLEOperationRead) -> bytearray | Literal[False]: ...

    @overload
    async def async_ble_operation(self, op: BLEOperationWrite) -> bool: ...

    async def async_ble_operation(self, op: BLEOperationRead | BLEOperationWrite) -> bool | bytearray:
        """Execute a BLE operation with proper connection management."""
        if self._client_lock.locked():
            return False

        try:
            async with asyncio.timeout(0.1):
                await self._client_lock.acquire()
        except TimeoutError:
            return False

        try:
            self._last_connection_attempt = time.time()
            max_attempts = MAX_RETRIES if op.retry else 1

            for attempt in range(max_attempts):
                result = await self._execute_single_ble_attempt(op, attempt)
                if result is not None:
                    return result

                if attempt < max_attempts - 1:
                    await asyncio.sleep(CONNECTION_DELAY)

            self._record_connection_failure()
            if self._consecutive_failures > 0 and self._consecutive_failures % 5 == 0:
                _LOGGER.debug("Device %s connection failed %d times in a row.", self.mac, self._consecutive_failures)
            return False

        finally:
            self._client_lock.release()

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
            except BleakError as err:
                _LOGGER.debug("BLE error reading characteristic %s: %s", key, err)
            except TimeoutError as err:
                _LOGGER.debug("Timeout reading characteristic %s: %s", key, err)
            except Exception:
                _LOGGER.exception("Unexpected error reading characteristic %s", key)

        return any_read_successful

    async def _attempt_device_info_read(self) -> dict[BaseStationDeviceInfoKey, str] | None:
        device = self.get_ble_device()
        if not device:
            return None

        info: dict[BaseStationDeviceInfoKey, str] = {}
        client_was_reused = False

        try:
            client, client_was_reused = await self._get_or_create_client(device)
            std_success = await self._read_standard_characteristics(client, info)
            spec_success = await self._read_specific_info(client, info)

        except BleakError as err:
            _LOGGER.debug("BLE error reading device info: %s", err)
        except TimeoutError as err:
            _LOGGER.debug("Timeout reading device info: %s", err)
        except Exception:
            _LOGGER.exception("Unexpected error reading device info")
        else:
            if not client_was_reused:
                await self._disconnect_client()
                await asyncio.sleep(0.5)

            if std_success or spec_success:
                return info
        finally:
            if not client_was_reused:
                await self._disconnect_client()

        return None

    async def read_device_info(self, /, *, force: bool = False) -> dict[BaseStationDeviceInfoKey, str]:
        """Read device information characteristics."""
        current_time = time.time()

        if (
            not force
            and self._device_info_read_success
            and (current_time - self._last_device_info_read < self.info_scan_interval)
        ):
            return self._info

        if self._client_lock.locked():
            return self._info

        try:
            async with asyncio.timeout(0.1):
                await self._client_lock.acquire()
        except TimeoutError:
            return self._info

        try:
            for attempt in range(INFO_READ_RETRIES):
                if attempt > 0:
                    await asyncio.sleep(CONNECTION_DELAY * (2**attempt))

                self._last_connection_attempt = time.time()
                info = await self._attempt_device_info_read()

                if info:
                    self._info |= info
                    self._record_connection_success()
                    self._last_device_info_read = current_time
                    self._device_info_read_success = True
                    return info

                self._record_connection_failure()

        finally:
            self._client_lock.release()

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
        info_scan_interval: int = DEFAULT_INFO_SCAN_INTERVAL,
    ) -> None:
        """Initialize the Valve basestation device."""
        super().__init__(hass, mac, name, connection_timeout, info_scan_interval)
        self._target_power_state: int | None = None
        self._target_state_expires = 0.0

    @property
    def default_name(self) -> str:
        """Return the default name."""
        return "Valve Basestation"

    @property
    def is_in_standby(self) -> bool:
        """Return True if device is in standby mode."""
        return self._last_power_state == BasestationPowerState.STANDBY

    async def turn_on(self) -> None:
        """Turn on the device."""
        if self._last_power_state == BasestationPowerState.ON:
            return

        self._target_power_state = BasestationPowerState.STARTING_UP
        self._target_state_expires = time.time() + 15.0
        self._update_power_state(BasestationPowerState.STARTING_UP)

        await self.async_ble_operation(
            BLEOperationWrite(
                V2_PWR_CHARACTERISTIC,
                bytes([BasestationPowerState.STARTING_UP]),
                without_response=True,
                repeat_count=3,
                repeat_delay=1.0,
                keep_alive=True,
            )
        )

    async def turn_off(self) -> None:
        """Turn off the device."""
        if self._last_power_state == BasestationPowerState.SLEEP:
            return

        self._target_power_state = BasestationPowerState.SLEEP
        self._target_state_expires = time.time() + 15.0
        self._update_power_state(BasestationPowerState.SLEEP)

        await self.async_ble_operation(
            BLEOperationWrite(
                V2_PWR_CHARACTERISTIC,
                bytes([BasestationPowerState.SLEEP]),
                without_response=True,
                repeat_count=3,
                repeat_delay=1.0,
                keep_alive=True,
            )
        )

    async def update(self) -> None:
        """Update the device state."""
        ble_device = self.get_ble_device()
        self._available = ble_device is not None

        if not self._available:
            return

        booting_states = (
            BasestationPowerState.STARTING_UP,
            BasestationPowerState.BOOTING_1,
            BasestationPowerState.BOOTING_2,
        )

        is_booting = self._last_power_state in booting_states or self._target_power_state is not None

        value = await self.async_ble_operation(BLEOperationRead(V2_PWR_CHARACTERISTIC, keep_alive=is_booting))

        if value and len(value) > 0:
            new_state = value[0]
            current_time = time.time()

            if self._target_power_state is not None:
                if current_time >= self._target_state_expires:
                    self._target_power_state = None
                else:
                    active_states = booting_states + (BasestationPowerState.ON,)

                    if new_state == self._target_power_state or (
                        self._target_power_state in active_states and new_state in active_states
                    ):
                        self._target_power_state = None
                    else:
                        return

            self._update_power_state(new_state)

            is_stable_now = new_state not in booting_states and self._target_power_state is None

            if is_booting and is_stable_now:
                try:
                    async with asyncio.timeout(0.5):
                        async with self._client_lock:
                            await self._disconnect_client()
                except TimeoutError:
                    pass

    async def set_standby(self) -> None:
        """Set the device to standby mode."""
        if self._last_power_state == BasestationPowerState.STANDBY:
            return

        self._target_power_state = BasestationPowerState.STANDBY
        self._target_state_expires = time.time() + 15.0
        self._update_power_state(BasestationPowerState.STANDBY)

        await self.async_ble_operation(
            BLEOperationWrite(
                V2_PWR_CHARACTERISTIC,
                bytes([BasestationPowerState.STANDBY]),
                without_response=True,
                repeat_count=3,
                repeat_delay=1.0,
                keep_alive=True,
            )
        )

    async def identify(self) -> None:
        """Make the device blink its LED to identify it."""
        await self.async_ble_operation(
            BLEOperationWrite(
                V2_IDENTIFY_CHARACTERISTIC,
                V2Command.IDENTIFY.value,
                without_response=True,
                repeat_count=3,
                repeat_delay=1.0,
            )
        )

    async def _read_specific_info(
        self, client: BleakClientWithServiceCache, info: dict[BaseStationDeviceInfoKey, Any]
    ) -> bool:
        try:
            channel = await client.read_gatt_char(V2_CHANNEL_CHARACTERISTIC)
            if channel:
                info["channel"] = int.from_bytes(channel, byteorder="big")
                return True
        except BleakError as err:
            _LOGGER.debug("BLE error reading channel: %s", err)
        except TimeoutError as err:
            _LOGGER.debug("Timeout reading channel: %s", err)
        except Exception:
            _LOGGER.exception("Unexpected error reading channel")

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
        info_scan_interval: int = DEFAULT_INFO_SCAN_INTERVAL,
    ) -> None:
        """Initialize the Vive basestation device."""
        super().__init__(hass, mac, name, connection_timeout, info_scan_interval)
        self.pair_id = pair_id
        if pair_id is not None:
            self._info["pair_id"] = f"0x{pair_id:08X}"

    @property
    def default_name(self) -> str:
        """Return the default name."""
        return "Vive Basestation"

    async def turn_on(self) -> None:
        """Turn on the device."""
        if not self.pair_id or self._is_on:
            return

        try:
            command = bytearray(20)
            command[0:4] = V1Command.TURN_ON.value
            command[4:8] = struct.pack("<I", int(self.pair_id))

            if await self.async_ble_operation(
                BLEOperationWrite(
                    V1_PWR_CHARACTERISTIC, bytes(command), without_response=True, repeat_count=3, repeat_delay=1.0
                )
            ):
                self._is_on = True
        except (ValueError, struct.error):
            _LOGGER.exception("Invalid pair_id format for V1 basestation %s", self.mac)
        except Exception:
            _LOGGER.exception("Unexpected error turning on V1 basestation")

    async def turn_off(self) -> None:
        """Turn off the device."""
        if not self.pair_id or not self._is_on:
            return

        try:
            command = bytearray(20)
            command[0:4] = V1Command.TURN_OFF.value
            command[4:8] = struct.pack("<I", int(self.pair_id))

            if await self.async_ble_operation(
                BLEOperationWrite(
                    V1_PWR_CHARACTERISTIC, bytes(command), without_response=True, repeat_count=3, repeat_delay=1.0
                )
            ):
                self._is_on = False
        except (ValueError, struct.error):
            _LOGGER.exception("Invalid pair_id format for V1 basestation %s", self.mac)
        except Exception:
            _LOGGER.exception("Unexpected error turning off V1 basestation")

    async def update(self) -> None:
        """Update the device state."""
        try:
            ble_device = self.get_ble_device()
            self._available = ble_device is not None
        except Exception:
            _LOGGER.exception("Error updating V1 basestation availability")
            self._available = False

    async def _read_specific_info(
        self, _client: BleakClientWithServiceCache, info: dict[BaseStationDeviceInfoKey, Any]
    ) -> bool:
        if self.pair_id:
            info["pair_id"] = f"0x{self.pair_id:08X}"
            return True
        return False


def get_basestation_device(
    hass: HomeAssistant,
    mac: str,
    device_type: str,
    name: str | None = None,
    pair_id: int | None = None,
    **kwargs: Any,
) -> BasestationDevice:
    """Create the appropriate device based on the device info."""
    connection_timeout = kwargs.get("connection_timeout", DEFAULT_CONNECTION_TIMEOUT)
    info_scan_interval = kwargs.get("info_scan_interval", DEFAULT_INFO_SCAN_INTERVAL)

    if device_type == DEVICE_TYPE_V2 or (name and name.startswith(V2_NAME_PREFIX)):
        return ValveBasestationDevice(
            hass, mac, name, connection_timeout=connection_timeout, info_scan_interval=info_scan_interval
        )

    if device_type == DEVICE_TYPE_V1 or (name and name.startswith(V1_NAME_PREFIX)):
        return ViveBasestationDevice(
            hass, mac, name, pair_id, connection_timeout=connection_timeout, info_scan_interval=info_scan_interval
        )

    return ValveBasestationDevice(
        hass, mac, name, connection_timeout=connection_timeout, info_scan_interval=info_scan_interval
    )


async def connect_delay(attempt: int) -> None:
    """Delay based on prior connection attempts."""
    if attempt > 0:
        await asyncio.sleep(CONNECTION_DELAY * (2**attempt))
    await asyncio.sleep(CONNECTION_DELAY * 0.5)
