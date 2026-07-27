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
from bleak_retry_connector import (
    BLEAK_OUT_OF_SLOTS_BACKOFF_TIME,
    BleakClientWithServiceCache,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)
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
UNAVAILABLE_AFTER_CONSECUTIVE_FAILURES = 3

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
    repeat_count: int = 1
    repeat_delay: float = 1.0


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
        self._last_error_out_of_slots = False

        # Tracks an in-flight establish_connection() call that outlives our own willingness to
        # wait for it - see _await_connection().
        self._pending_connect_task: asyncio.Task[BleakClientWithServiceCache] | None = None
        self._pending_connect_waiters = 0

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
    def last_power_state_age(self) -> float | None:
        """Return seconds since last_power_state was last confirmed by a read, or None if never set."""
        if self._last_power_state is None:
            return None
        return time.time() - self._last_power_state_update

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

    async def cleanup(self) -> None:
        """Clean up resources when device is being removed."""
        # Attempt to acquire lock with timeout
        acquired_lock = False
        try:
            async with asyncio.timeout(2.0):
                await self._client_lock.acquire()
                acquired_lock = True
        except TimeoutError:
            _LOGGER.debug("Timeout acquiring lock during cleanup for %s", self.mac)

        try:
            # Disconnect if we have a client connection
            client_to_disconnect = None
            if self._current_client and self._current_client.is_connected:
                client_to_disconnect = self._current_client
            self._current_client = None

            if client_to_disconnect:
                try:
                    async with asyncio.timeout(5.0):
                        await client_to_disconnect.disconnect()
                except (TimeoutError, Exception) as e:
                    _LOGGER.debug("Error disconnecting client during cleanup: %s", e)
        finally:
            if acquired_lock:
                self._client_lock.release()

        # A connect attempt from a just-abandoned poll may still be running in the background (see
        # _await_connection) - give it a short grace period to finish and be reaped by
        # _on_connect_task_done rather than tearing the entry down while it's still in flight. Not
        # cancelling it even here: if it doesn't finish in time, it'll still get closed by
        # _on_connect_task_done whenever it does, just without this method waiting around for it.
        if (task := self._pending_connect_task) and not task.done():
            try:
                async with asyncio.timeout(5.0):
                    await asyncio.shield(task)
            except Exception as e:
                _LOGGER.debug("Pending connect for %s did not finish during cleanup: %s", self.mac, e)

        self._available = False

    def _record_connection_success(self) -> None:
        self._consecutive_failures = 0
        self._retry_count = 0
        self._available = True
        self._last_successful_connection = time.time()
        self._last_error_out_of_slots = False

    def _record_connection_failure(self) -> None:
        self._consecutive_failures += 1
        self._retry_count += 1
        if self._consecutive_failures >= UNAVAILABLE_AFTER_CONSECUTIVE_FAILURES:
            self._available = False

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
        client = None
        result: bool | bytearray | None = None

        try:
            await connect_delay(attempt)

            device = self.get_ble_device()
            if not device:
                return None

            client = await asyncio.wait_for(self._await_connection(device), timeout=self.connection_timeout)

            async with client:
                self._current_client = client
                result = await self._perform_ble_operation(client, op)

                self._record_connection_success()
                await client.disconnect()

            # Delay to allow BLE Proxy to internally clear the connection slot
            await asyncio.sleep(0.5)

        except Exception as err:
            self._record_connect_exception(err, "contacting")
        else:
            return result
        finally:
            if client and client.is_connected:
                try:
                    await client.disconnect()
                    await asyncio.sleep(0.5)
                except Exception as err:
                    _LOGGER.debug("Ignored disconnect error: %s", err)
            self._current_client = None

        return None

    async def _await_connection(self, device: BLEDevice) -> BleakClientWithServiceCache:
        """
        Wait for a connected client without ever cancelling the underlying connect attempt.

        establish_connection() has its own careful cleanup/backoff for a slow or misbehaving BLE
        proxy (see bleak_retry_connector's BLEAK_SAFETY_TIMEOUT), but that logic only runs if it's
        allowed to finish naturally - CancelledError isn't one of the exceptions its retry loop
        catches, so cancelling it from outside (e.g. because our own connection_timeout elapsed)
        skips that cleanup entirely and can leave an ESPHome proxy holding a connection slot
        forever, regardless of how generous the timeout is. So the connect itself is shielded:
        giving up here only stops us *waiting* for it, never the attempt itself. If another
        attempt (or the next poll cycle) comes looking for a connection before this one resolves,
        it reuses this same in-flight task instead of racing a second, competing connection to the
        same device. Whatever happens if nobody ends up waiting for it is handled by
        _on_connect_task_done/_close_abandoned_client.
        """
        task = self._pending_connect_task
        if task is None or task.done():
            task = self.hass.async_create_background_task(
                establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    device.name or device.address,
                    disconnected_callback=self._handle_disconnect,
                    max_attempts=1,
                    use_services_cache=True,
                ),
                name=f"basestation_connect_{self.mac}",
            )
            task.add_done_callback(self._on_connect_task_done)
            self._pending_connect_task = task

        self._pending_connect_waiters += 1
        try:
            return await asyncio.shield(task)
        finally:
            self._pending_connect_waiters -= 1

    def _on_connect_task_done(self, task: asyncio.Task[BleakClientWithServiceCache]) -> None:
        """
        Handle a connect attempt that finished after everyone waiting on it already gave up.

        If it succeeded, we now own a live connection nobody asked for anymore - close it right
        away so it can't sit there occupying a proxy slot. If it failed, establish_connection()
        already ran its own cleanup before raising, so there's nothing left to do.
        """
        if self._pending_connect_task is task:
            self._pending_connect_task = None

        if self._pending_connect_waiters > 0:
            return

        try:
            client = task.result()
        except (Exception, asyncio.CancelledError):
            return

        self.hass.async_create_background_task(
            self._close_abandoned_client(client), name=f"basestation_close_abandoned_{self.mac}"
        )

    async def _close_abandoned_client(self, client: BleakClientWithServiceCache) -> None:
        """Disconnect a client that only finished connecting after we stopped waiting for it."""
        try:
            if client.is_connected:
                await client.disconnect()
        except Exception as err:
            _LOGGER.debug("Error disconnecting abandoned client for %s: %s", self.mac, err)

    @overload
    async def async_ble_operation(self, op: BLEOperationRead) -> bytearray | Literal[False]: ...

    @overload
    async def async_ble_operation(self, op: BLEOperationWrite) -> bool: ...

    async def async_ble_operation(self, op: BLEOperationRead | BLEOperationWrite) -> bool | bytearray:
        """Execute a BLE operation with proper connection management."""
        lock_timeout = 20.0 if isinstance(op, BLEOperationWrite) else 10.0

        try:
            async with asyncio.timeout(lock_timeout):
                await self._client_lock.acquire()
        except TimeoutError:
            _LOGGER.debug(
                "Timeout (%ss) acquiring lock for BLE operation %s on %s",
                lock_timeout,
                "write" if isinstance(op, BLEOperationWrite) else "read",
                self.mac,
            )
            return False

        try:
            self._last_connection_attempt = time.time()
            max_attempts = MAX_RETRIES if op.retry else 1

            for attempt in range(max_attempts):
                result = await self._execute_single_ble_attempt(op, attempt)
                if result is not None:
                    return result

                if attempt < max_attempts - 1:
                    delay = BLEAK_OUT_OF_SLOTS_BACKOFF_TIME if self._last_error_out_of_slots else CONNECTION_DELAY
                    await asyncio.sleep(delay)

            self._record_connection_failure()
            if self._consecutive_failures > 0 and self._consecutive_failures % 5 == 0:
                _LOGGER.debug("Device %s connection failed %d times in a row.", self.mac, self._consecutive_failures)
            return False

        finally:
            self._client_lock.release()

    def _handle_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.debug("Device %s disconnected", self.mac)

    def _record_connect_exception(self, err: Exception, context: str) -> None:
        """Classify a connect/operate exception, log it, and flag out-of-slots for backoff."""
        if isinstance(err, BleakOutOfConnectionSlotsError):
            self._last_error_out_of_slots = True
            _LOGGER.warning(
                "BLE proxy/adapter out of connection slots while %s %s. Consider adding another "
                "ESPHome Bluetooth proxy near this device: %s",
                context,
                self.mac,
                err,
            )
            return

        self._last_error_out_of_slots = False
        if isinstance(err, BleakError):
            _LOGGER.debug("BLE error %s %s: %s", context, self.mac, err)
        elif isinstance(err, TimeoutError):
            _LOGGER.debug("Timeout %s %s: %s", context, self.mac, err)
        else:
            _LOGGER.exception("Unexpected error %s %s", context, self.mac)

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
        client = None
        std_success = False
        spec_success = False

        try:
            client = await asyncio.wait_for(self._await_connection(device), timeout=self.connection_timeout)

            async with client:
                self._current_client = client

                std_success = await self._read_standard_characteristics(client, info)
                spec_success = await self._read_specific_info(client, info)

                if std_success or spec_success:
                    await client.disconnect()
                    await asyncio.sleep(0.5)

        except Exception as err:
            self._record_connect_exception(err, "reading device info for")
        else:
            if std_success or spec_success:
                return info
        finally:
            if client and client.is_connected:
                try:
                    await client.disconnect()
                    await asyncio.sleep(0.5)
                except Exception as err:
                    _LOGGER.debug("Ignored disconnect error: %s", err)

            self._current_client = None

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

        lock_timeout = 15.0 if not self._device_info_read_success else 5.0

        try:
            async with asyncio.timeout(lock_timeout):
                await self._client_lock.acquire()
        except TimeoutError:
            _LOGGER.debug("Timeout (%ss) acquiring lock for reading device info on %s", lock_timeout, self.mac)
            return self._info

        try:
            for attempt in range(INFO_READ_RETRIES):
                if attempt > 0:
                    delay = (
                        BLEAK_OUT_OF_SLOTS_BACKOFF_TIME
                        if self._last_error_out_of_slots
                        else CONNECTION_DELAY * (2**attempt)
                    )
                    await asyncio.sleep(delay)

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
        """Read device information specific to a basestation model."""


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
            )
        )

    async def update(self) -> None:
        """Update the device state."""
        ble_device = self.get_ble_device()
        if ble_device is None:
            # Not visible at all: definitely unavailable. Otherwise, leave availability to
            # _record_connection_success/_record_connection_failure below, so a device that is
            # advertising but repeatedly failing to connect still ends up marked unavailable.
            self._available = False
            return

        value = await self.async_ble_operation(BLEOperationRead(V2_PWR_CHARACTERISTIC))
        if value and len(value) > 0:
            new_state = value[0]
            current_time = time.time()

            if self._target_power_state is not None and current_time < self._target_state_expires:
                active_states = (
                    BasestationPowerState.STARTING_UP,
                    BasestationPowerState.BOOTING_1,
                    BasestationPowerState.BOOTING_2,
                    BasestationPowerState.ON,
                )

                # Wenn der angestrebte Status oder ein logischer Folgestatus erreicht wurde
                if new_state == self._target_power_state or (
                    self._target_power_state in active_states and new_state in active_states
                ):
                    self._target_power_state = None
                else:
                    return

            self._update_power_state(new_state)

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
        # V1 has no readable state characteristic, so advertisement visibility is the only signal
        # available here. Don't let it override a failure-driven unavailable state (see
        # _record_connection_failure) once repeated command failures have marked us unavailable.
        try:
            ble_device = self.get_ble_device()
            if ble_device is None:
                self._available = False
            elif self._consecutive_failures < UNAVAILABLE_AFTER_CONSECUTIVE_FAILURES:
                self._available = True
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
    """
    Delay based on prior connection attempts.

    Implements exponential backoff for connection retries to reduce load on BLE devices.

    Args:
        attempt: The retry attempt number (0 for first attempt)

    """
    if attempt > 0:
        await asyncio.sleep(CONNECTION_DELAY * (2**attempt))
    await asyncio.sleep(CONNECTION_DELAY * 0.5)
