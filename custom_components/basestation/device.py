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

from .connection_log import ConnectionLog, Outcome, Phase
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

# V1 has no characteristic to actively read a status from, so unlike V2 its update() poll can't
# naturally retry/self-heal on its own. Without this, once UNAVAILABLE_AFTER_CONSECUTIVE_FAILURES
# is hit the device stays unavailable forever, even after it's back in range, until a user happens
# to send a command that succeeds. This gives it another chance periodically instead.
UNAVAILABLE_RETRY_COOLDOWN = 300.0

# A connect attempt we've stopped waiting for is normal and harmless (see _await_connection), but
# one still running this long afterwards is not: establish_connection() only stays busy that long
# under sustained proxy congestion, and it's holding (or repeatedly grabbing) a slot the whole
# time. Warn once per attempt when it crosses this, since it's the earliest visible symptom of the
# proxy slot problem - well before the device is marked unavailable.
PENDING_CONNECT_WARN_AGE = 90.0

# How long a single BLE operation may hold the per-device client lock before we treat the holder
# as stuck rather than merely slow. Comfortably above a worst-case retrying operation
# (MAX_RETRIES attempts, each up to connection_timeout plus backoff) at default settings.
LOCK_HELD_WARN_AGE = 120.0

# Substrings of the message bleak_esphome raises while its ESPHome proxy is still (re)establishing
# its own API connection to Home Assistant (e.g. after a Wi-Fi drop or a firmware crash/reboot on
# the proxy itself). bleak_esphome collapses aioesphomeapi's structured APIConnectionError into a
# bare bleak.exc.BleakError (`raise BleakError(str(err)) from err`), so there's no exception type
# left to check by the time it reaches us - only the message, which as of this aioesphomeapi
# version reads "Authenticated connection not ready yet for <name> @ <ip>; current state is
# ConnectionState.<state>!". Matching on wording is inherently fragile and could break on an
# aioesphomeapi update, same caveat as the bleak_retry_connector internals imported above.
PROXY_RESTARTING_MARKERS = ("ConnectionState.", "not ready yet")

type BaseStationDeviceInfoKey = Literal["firmware", "model", "hardware", "manufacturer", "channel", "pair_id"]


def _is_proxy_restarting_error(err: Exception) -> bool:
    """Return True if err looks like PROXY_RESTARTING_MARKERS - see that constant for why."""
    message = str(err)
    return all(marker in message for marker in PROXY_RESTARTING_MARKERS)


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
        self._last_failure_time = 0.0
        self._last_successful_connection = 0.0
        self._current_client: BleakClientWithServiceCache | None = None
        self._client_lock = asyncio.Lock()
        self._last_error_out_of_slots = False

        # Tracks an in-flight establish_connection() call that outlives our own willingness to
        # wait for it - see _await_connection().
        self._pending_connect_task: asyncio.Task[BleakClientWithServiceCache] | None = None
        self._pending_connect_waiters = 0
        self._pending_connect_started = 0.0
        self._pending_connect_warned = False

        # Observability only - never consulted for control flow. See connection_log.py for why
        # this is kept in memory rather than relying on the log.
        self.connection_log = ConnectionLog()
        self._lock_acquired_at = 0.0
        self._lock_holder: str | None = None

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
    def pending_connect_age(self) -> float | None:
        """Return seconds the current in-flight connect has been running, or None if there is none."""
        task = self._pending_connect_task
        if task is None or task.done():
            return None
        return time.monotonic() - self._pending_connect_started

    @property
    def lock_held_age(self) -> float | None:
        """Return seconds the client lock has been held, or None if it is free."""
        if not self._client_lock.locked() or not self._lock_acquired_at:
            return None
        return time.monotonic() - self._lock_acquired_at

    @property
    def lock_holder(self) -> str | None:
        """Return a description of the operation currently holding the client lock, if any."""
        return self._lock_holder if self._client_lock.locked() else None

    @property
    def last_successful_connection(self) -> float | None:
        """Return the wall-clock time of the last successful connection, or None if never."""
        return self._last_successful_connection or None

    @property
    def consecutive_failures(self) -> int:
        """Return how many connection attempts have failed in a row."""
        return self._consecutive_failures

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

    def _seen_by_proxies(self) -> list[str]:
        """
        Return the names of scanners/proxies currently seeing this device's advertisements.

        Pulled into the "now unavailable" warning so a proxy-wide problem (one proxy crashing or
        losing Wi-Fi and taking every station behind it down at once) is visible directly in the
        log, rather than only discoverable afterwards by downloading diagnostics from every
        affected device and cross-referencing them by hand.
        """
        return [
            scanner_device.scanner.name
            for scanner_device in bluetooth.async_scanner_devices_by_address(self.hass, self.mac, connectable=True)
        ]

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

    def _set_available(self, reason: str, *, available: bool) -> None:
        """
        Update availability, logging and recording only actual transitions.

        Availability flipping is the symptom a user actually notices, so it's the one thing here
        that's worth surfacing above DEBUG - but only on a change, which makes it self-limiting
        (a station that's been down for hours logs once, not once per poll). The reason string is
        what makes the line useful afterwards: "unavailable" alone doesn't distinguish a station
        that was unplugged from one whose proxy ran out of slots.
        """
        if available == self._available:
            return

        self._available = available

        if available:
            downtime = time.time() - self._last_failure_time if self._last_failure_time else None
            self.connection_log.record(Phase.AVAILABILITY, Outcome.AVAILABLE, detail=reason, duration=downtime)
            _LOGGER.info(
                "Basestation %s is available again (%s)%s",
                self.mac,
                reason,
                f", after {downtime:.0f}s unavailable" if downtime else "",
            )
            return

        self.connection_log.stats.became_unavailable += 1
        self.connection_log.record(Phase.AVAILABILITY, Outcome.UNAVAILABLE, detail=reason)

        last_failure = self.connection_log.last_failure
        seen_by = self._seen_by_proxies()
        _LOGGER.warning(
            "Basestation %s is now unavailable (%s). Consecutive failures: %d. Last error: %s. "
            "In-flight connect: %s. Suspected stranded proxy slots so far: %d. Currently seen by: %s. "
            "Download this device's diagnostics from its device page for the full history",
            self.mac,
            reason,
            self._consecutive_failures,
            last_failure.detail if last_failure else "none recorded",
            f"{self.pending_connect_age:.0f}s old" if self.pending_connect_age is not None else "none",
            self.connection_log.stats.suspected_stranded_slots,
            ", ".join(seen_by) if seen_by else "no scanner/proxy currently",
        )

    def _record_connection_success(self) -> None:
        self._consecutive_failures = 0
        self._retry_count = 0
        self._set_available("operation succeeded", available=True)
        self._last_successful_connection = time.time()
        self._last_error_out_of_slots = False
        self.connection_log.stats.operation_succeeded += 1

    def _record_connection_failure(self) -> None:
        self._consecutive_failures += 1
        self._retry_count += 1
        self._last_failure_time = time.time()
        self.connection_log.stats.operation_failed += 1
        if self._consecutive_failures >= UNAVAILABLE_AFTER_CONSECUTIVE_FAILURES:
            self._set_available(f"{self._consecutive_failures} consecutive failures", available=False)

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
                await self._safe_disconnect(client, "completing operation")

            # Delay to allow BLE Proxy to internally clear the connection slot
            await asyncio.sleep(0.5)

        except Exception as err:
            self._record_connect_exception(err, "contacting")
        else:
            return result
        finally:
            if client and client.is_connected:
                await self._safe_disconnect(client, "cleaning up after operation")
                await asyncio.sleep(0.5)
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
            self._pending_connect_started = time.monotonic()
            self._pending_connect_warned = False
            self.connection_log.stats.connect_started += 1
        else:
            self.connection_log.stats.connect_reused += 1
            self._warn_if_connect_is_stuck()

        started = self._pending_connect_started
        self._pending_connect_waiters += 1
        try:
            client = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Our wait was cancelled - almost always connection_timeout elapsing in the caller,
            # not the connect itself being torn down. The attempt keeps running by design; record
            # it so a pattern of them is visible later, since a rising abandon count is what
            # precedes proxy slot exhaustion.
            if not task.cancelled():
                self.connection_log.stats.connect_abandoned += 1
                self.connection_log.record(
                    Phase.CONNECT,
                    Outcome.ABANDONED,
                    duration=time.monotonic() - started,
                    detail=f"{self._pending_connect_waiters - 1} other waiter(s) remaining",
                )
            raise
        except Exception as err:
            self.connection_log.stats.connect_failed += 1
            self.connection_log.record_exception(Phase.CONNECT, Outcome.ERROR, err, duration=time.monotonic() - started)
            raise
        else:
            self.connection_log.stats.connect_succeeded += 1
            self.connection_log.record(Phase.CONNECT, Outcome.OK, duration=time.monotonic() - started, store=False)
            return client
        finally:
            self._pending_connect_waiters -= 1

    def _warn_if_connect_is_stuck(self) -> None:
        """Warn once when an in-flight connect has been running implausibly long."""
        age = self.pending_connect_age
        if age is None or age < PENDING_CONNECT_WARN_AGE or self._pending_connect_warned:
            return

        self._pending_connect_warned = True
        self.connection_log.record(Phase.CONNECT, Outcome.TIMEOUT, detail="still in flight", duration=age)
        _LOGGER.warning(
            "Connect attempt for basestation %s has been in flight for %.0fs and is still running. "
            "This normally means the Bluetooth proxy covering it is congested or out of connection "
            "slots; it is being left to finish deliberately rather than cancelled, since cancelling "
            "it is what strands a slot. Out-of-slots errors so far: %d",
            self.mac,
            age,
            self.connection_log.stats.out_of_slots,
        )

    def _on_connect_task_done(self, task: asyncio.Task[BleakClientWithServiceCache]) -> None:
        """
        Handle a connect attempt that finished after everyone waiting on it already gave up.

        If it succeeded, we now own a live connection nobody asked for anymore - close it right
        away so it can't sit there occupying a proxy slot. If it failed, establish_connection()
        already ran its own cleanup before raising, so there's nothing left to do.
        """
        # A replacement attempt may already have started by the time this callback runs (done
        # callbacks are scheduled, not immediate), in which case _pending_connect_started belongs
        # to that one and says nothing about this task's age.
        age = time.monotonic() - self._pending_connect_started if self._pending_connect_task is task else None

        if self._pending_connect_task is task:
            self._pending_connect_task = None

        if self._pending_connect_waiters > 0:
            return

        try:
            client = task.result()
        except asyncio.CancelledError:
            self.connection_log.record(Phase.CONNECT, Outcome.CANCELLED, duration=age, detail="nobody waiting")
            return
        except Exception as err:
            # establish_connection() ran its own cleanup before raising, so the slot is not at
            # risk here - but the error is still worth keeping, because this is the one connect
            # outcome nobody is awaiting and it would otherwise vanish entirely.
            self.connection_log.record_exception(
                Phase.CONNECT, Outcome.ERROR, err, duration=age, context="nobody waiting"
            )
            return

        _LOGGER.info(
            "Connect for basestation %s succeeded%s, once nothing was waiting for it any more; "
            "disconnecting it so it cannot hold a proxy connection slot",
            self.mac,
            f" after {age:.0f}s" if age is not None else "",
        )
        self.hass.async_create_background_task(
            self._close_abandoned_client(client), name=f"basestation_close_abandoned_{self.mac}"
        )

    async def _close_abandoned_client(self, client: BleakClientWithServiceCache) -> None:
        """Disconnect a client that only finished connecting after we stopped waiting for it."""
        try:
            if client.is_connected:
                await client.disconnect()
        except Exception as err:
            # The connection is up and we cannot close it: this is the failure that actually
            # leaks an ESPHome proxy slot, so it warrants a warning rather than the silence it
            # used to get.
            self.connection_log.stats.abandoned_close_failed += 1
            self.connection_log.record_exception(Phase.DISCONNECT, Outcome.STRANDED, err)
            _LOGGER.warning(
                "Failed to disconnect an abandoned connection to basestation %s: %s. A Bluetooth proxy "
                "connection slot may now be stuck until the proxy is restarted",
                self.mac,
                err,
            )
        else:
            self.connection_log.stats.abandoned_reclaimed += 1
            self.connection_log.record(Phase.DISCONNECT, Outcome.RECLAIMED)

    async def _safe_disconnect(self, client: BleakClientWithServiceCache, context: str) -> None:
        """
        Disconnect a client, recording anything that stops it from completing.

        Nothing here changes the outcome - it's purely so that a disconnect which fails, or which
        gets torn down part-way through, leaves a trace. That matters because these calls are the
        one connection path still reachable by an enclosing timeout: unlike the initial connect
        (which _await_connection shields), a disconnect running when the coordinator's overall
        update timeout expires is cancelled where it stands, which is a plausible way to leave an
        ESPHome proxy holding a slot. If that is what's happening, `disconnect_cancelled` in the
        diagnostics will be non-zero and will have climbed at the moment the device went
        unavailable.
        """
        started = time.monotonic()
        try:
            await client.disconnect()
        except asyncio.CancelledError:
            self.connection_log.stats.disconnect_cancelled += 1
            self.connection_log.record(
                Phase.DISCONNECT,
                Outcome.CANCELLED,
                duration=time.monotonic() - started,
                detail=context,
            )
            _LOGGER.warning(
                "Disconnect from basestation %s was cancelled after %.1fs while %s. The connection was "
                "torn down mid-teardown, which can leave a Bluetooth proxy connection slot occupied",
                self.mac,
                time.monotonic() - started,
                context,
            )
            raise
        except Exception as err:
            self.connection_log.stats.disconnect_failed += 1
            self.connection_log.record_exception(
                Phase.DISCONNECT, Outcome.ERROR, err, duration=time.monotonic() - started, context=context
            )
            _LOGGER.debug("Ignored disconnect error for %s while %s: %s", self.mac, context, err)
        else:
            self.connection_log.record(Phase.DISCONNECT, Outcome.OK, duration=time.monotonic() - started, store=False)

    @overload
    async def async_ble_operation(self, op: BLEOperationRead) -> bytearray | Literal[False]: ...

    @overload
    async def async_ble_operation(self, op: BLEOperationWrite) -> bool: ...

    async def async_ble_operation(self, op: BLEOperationRead | BLEOperationWrite) -> bool | bytearray:
        """Execute a BLE operation with proper connection management."""
        lock_timeout = 20.0 if isinstance(op, BLEOperationWrite) else 10.0
        holder = f"{'write' if isinstance(op, BLEOperationWrite) else 'read'} {op.characteristic_uuid}"

        if not await self._acquire_lock(lock_timeout, holder):
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
            self._release_lock()

    async def _acquire_lock(self, timeout: float, holder: str) -> bool:  # noqa: ASYNC109
        """
        Take the per-device client lock, recording who holds it and reporting failure to get it.

        Knowing *what* was holding the lock is the difference between a usable and a useless
        report when one station wedges while the rest keep working: the timeout alone says only
        that something was stuck, whereas the holder plus how long it had been held points
        straight at the operation that never returned.

        The timeout is a parameter rather than an `asyncio.timeout` at the call site (ASYNC109)
        because expiring it is not an error to propagate: it is recorded and logged here, and the
        caller just gets False.
        """
        try:
            async with asyncio.timeout(timeout):
                await self._client_lock.acquire()
        except TimeoutError:
            self.connection_log.stats.lock_timeout += 1
            held_by, held_for = self._lock_holder, self.lock_held_age
            self.connection_log.record(
                Phase.LOCK,
                Outcome.TIMEOUT,
                duration=timeout,
                detail=f"{holder} blocked by {held_by or 'unknown'}",
            )
            _LOGGER.warning(
                "Timed out after %.0fs waiting for the Bluetooth lock on basestation %s to run '%s'. "
                "It is held by '%s'%s, which has not finished. Repeated occurrences mean an operation "
                "is wedged and the device will go unavailable",
                timeout,
                self.mac,
                holder,
                held_by or "unknown",
                f" for {held_for:.0f}s" if held_for is not None else "",
            )
            return False
        else:
            self._lock_acquired_at = time.monotonic()
            self._lock_holder = holder
            return True

    def _release_lock(self) -> None:
        """Release the client lock, warning if the operation that held it took implausibly long."""
        held_for = self.lock_held_age
        holder = self._lock_holder

        self._lock_acquired_at = 0.0
        self._lock_holder = None
        self._client_lock.release()

        if held_for is not None and held_for > LOCK_HELD_WARN_AGE:
            self.connection_log.record(Phase.LOCK, Outcome.TIMEOUT, duration=held_for, detail=f"slow: {holder}")
            _LOGGER.warning(
                "Bluetooth operation '%s' on basestation %s held the connection lock for %.0fs. "
                "Everything else queued behind it for that entire time",
                holder,
                self.mac,
                held_for,
            )

    def _handle_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.debug("Device %s disconnected", self.mac)

    def _record_connect_exception(self, err: Exception, context: str) -> None:
        """Classify a connect/operate exception, log it, and flag out-of-slots for backoff."""
        if isinstance(err, BleakOutOfConnectionSlotsError):
            self._last_error_out_of_slots = True
            self.connection_log.stats.out_of_slots += 1
            self.connection_log.record_exception(Phase.OPERATION, Outcome.OUT_OF_SLOTS, err, context=context)
            _LOGGER.warning(
                "BLE proxy/adapter out of connection slots while %s %s (%d time(s) so far). Consider adding "
                "another ESPHome Bluetooth proxy near this device: %s",
                context,
                self.mac,
                self.connection_log.stats.out_of_slots,
                err,
            )
            return

        self._last_error_out_of_slots = False

        if isinstance(err, BleakError) and _is_proxy_restarting_error(err):
            self.connection_log.stats.proxy_restarting += 1
            self.connection_log.record_exception(Phase.OPERATION, Outcome.PROXY_RESTARTING, err, context=context)
            _LOGGER.warning(
                "The Bluetooth proxy covering basestation %s appears to still be (re)connecting to "
                "Home Assistant itself (%d time(s) so far) while %s: %s. This is a proxy-side hiccup "
                "(Wi-Fi drop, or the proxy device rebooting/crashing) rather than proxy congestion - "
                "check that proxy's own logs if this keeps happening",
                self.mac,
                self.connection_log.stats.proxy_restarting,
                context,
                err,
            )
            return

        if isinstance(err, BleakError):
            self.connection_log.record_exception(Phase.OPERATION, Outcome.ERROR, err, context=context)
            _LOGGER.debug("BLE error %s %s: %s", context, self.mac, err)
        elif isinstance(err, TimeoutError):
            self.connection_log.record_exception(Phase.OPERATION, Outcome.TIMEOUT, err, context=context)
            _LOGGER.debug("Timeout %s %s: %s", context, self.mac, err)
        else:
            self.connection_log.record_exception(Phase.OPERATION, Outcome.ERROR, err, context=context)
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
                    await self._safe_disconnect(client, "completing device info read")
                    await asyncio.sleep(0.5)

        except Exception as err:
            self._record_connect_exception(err, "reading device info for")
        else:
            if std_success or spec_success:
                return info
        finally:
            if client and client.is_connected:
                await self._safe_disconnect(client, "cleaning up after device info read")
                await asyncio.sleep(0.5)

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

        if not await self._acquire_lock(lock_timeout, "device info read"):
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
            self._release_lock()

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
            self._set_available("no longer advertising to any Bluetooth adapter or proxy", available=False)
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

    def restore_is_on(self, *, is_on: bool) -> None:
        """
        Restore the last known on/off state after a restart.

        V1 has no characteristic to read this back from the hardware, so _is_on is only ever our
        own best guess from the last command we successfully sent - restoring it is strictly
        better than the fresh-object default of False, which would otherwise make the switch show
        "off" for a station that's actually on until the user happens to press it.
        """
        self._is_on = is_on

    async def turn_on(self) -> None:
        """Turn on the device."""
        # Deliberately not early-returning when _is_on already looks True: unlike V2, that flag is
        # never confirmed by an actual read, so trusting it as a hard gate risks silently
        # swallowing a legitimate command whenever it's stale (e.g. right after a restart, before
        # restore_is_on() runs, or if the station was toggled by something other than this
        # integration). Sending an on/off command the station is already in is harmless.
        if not self.pair_id:
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
        # See turn_on() for why this doesn't early-return based on _is_on.
        if not self.pair_id:
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
        # available here - this never performs an actual BLE operation, unlike V2's update(), so
        # unlike V2 it can't self-heal by simply succeeding on the next poll. Once
        # _record_connection_failure has marked us unavailable, only give it another chance after
        # UNAVAILABLE_RETRY_COOLDOWN has passed since the last failure, rather than requiring a
        # user to happen to send a command that succeeds - otherwise a station that came back into
        # range would stay marked unavailable indefinitely.
        try:
            ble_device = self.get_ble_device()
            if ble_device is None:
                self._set_available("no longer advertising to any Bluetooth adapter or proxy", available=False)
                return

            if self._consecutive_failures >= UNAVAILABLE_AFTER_CONSECUTIVE_FAILURES:
                if time.time() - self._last_failure_time < UNAVAILABLE_RETRY_COOLDOWN:
                    return
                self._consecutive_failures = 0

            self._set_available("advertising", available=True)
        except Exception:
            _LOGGER.exception("Error updating V1 basestation availability")
            self._set_available("error while checking advertisement visibility", available=False)

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
