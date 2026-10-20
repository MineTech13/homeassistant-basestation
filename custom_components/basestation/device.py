"""Device classes for basestation integration."""

import asyncio
import logging
import struct
import time
from abc import ABC, abstractmethod
from collections import deque
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from attr import dataclass
from bleak.backends.device import BLEDevice
from bleak.exc import BleakCharacteristicNotFoundError, BleakError
from bleak_retry_connector import (
    BLEAK_OUT_OF_SLOTS_BACKOFF_TIME,
    BleakClientWithServiceCache,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from .connection_log import ConnectionLog, Outcome, Phase
from .const import (
    BOOTING_STATES,
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
    V1_READBACK_OFF,
    V1_READBACK_ON,
    V1_READBACK_RESTARTED,
    V2_CHANNEL_CHARACTERISTIC,
    V2_IDENTIFY_CHARACTERISTIC,
    V2_NAME_PREFIX,
    V2_PWR_CHARACTERISTIC,
    BasestationPowerState,
    V1Command,
    V2Command,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

_LOGGER = logging.getLogger(__name__)

# Constants
CONNECTION_DELAY = 0.5
MAX_RETRIES = 2
INFO_READ_RETRIES = 3
UNAVAILABLE_AFTER_CONSECUTIVE_FAILURES = 3

# A V1 write without response is only queued locally when write_gatt_char() returns, so stay
# connected this long after it before reading it back or disconnecting (as ShayBox/Lighthouse does).
V1_SETTLE_DELAY = 1.0
# A V1 command is checked by reading the power characteristic back (see decode_v1_readback) and
# rewritten until the station shows it, at most this many writes in total: the three writes V1 always
# got before the read-back existed. A read-back that fails leaves it at the one write, which is also
# all ShayBox/Lighthouse sends.
V1_COMMAND_ATTEMPTS = 3

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

# After a connect times out, polling (not user commands) leaves the station alone for this long.
# ESPHome proxies log "OPEN_EVT in unexpected state" when the controller completes a link just as
# the host gives up on it (bleak_esphome's own 20s connect timeout), and a station left connected
# that way stops advertising until the proxy is restarted. On 2026-10-04 one station took seven
# back-to-back connect attempts in 3.5 minutes (poll retries plus the info read), each one ending
# like that, before it vanished from both proxies. Spacing retries out cuts the chances.
CONNECT_TIMEOUT_COOLDOWN = 180.0

# Message of the bleak_esphome connect failure that ends in that OPEN_EVT race. Our own
# connection_timeout elapsing (a bare TimeoutError while still waiting to connect) counts too.
CONNECT_TIMEOUT_MARKERS = ("Timeout waiting for connect response",)

# GATT error an ESPHome proxy returns for a read on a connection it just reported as established.
# Seen on 2026-10-04 from a proxy whose Bluedroid stack had wedged on one address: every connect
# "succeeded" in ~0.2s, the first read failed with this, and the proxy then logged "Timeout
# waiting for teardown". Same message-text fragility caveat as PROXY_RESTARTING_MARKERS.
PHANTOM_CONNECTION_MARKERS = ("Not connected",)

# How many connect failures to charge a proxy with for one phantom connection (see
# _penalise_connected_proxy). habluetooth scores a connection path as RSSI minus
# 0.51 * rssi_diff * failures, so two flips the choice between two proxies only while the other one
# carries no penalty of its own (connects in progress, its own failures, last free slot).
PHANTOM_CONNECTION_PENALTY = 2

type BaseStationDeviceInfoKey = Literal["firmware", "model", "hardware", "manufacturer", "channel", "pair_id"]


def _is_proxy_restarting_error(err: Exception) -> bool:
    """Return True if err looks like PROXY_RESTARTING_MARKERS - see that constant for why."""
    message = str(err)
    return all(marker in message for marker in PROXY_RESTARTING_MARKERS)


def _is_connect_timeout_error(err: Exception) -> bool:
    """Return True if a failed connect timed out - see CONNECT_TIMEOUT_COOLDOWN for why it matters."""
    if isinstance(err, TimeoutError):
        return True
    message = str(err)
    return any(marker in message for marker in CONNECT_TIMEOUT_MARKERS)


def _is_phantom_connection_error(err: Exception) -> bool:
    """Return True if err is a proxy denying a connection it just reported - see PHANTOM_CONNECTION_MARKERS."""
    message = str(err)
    return isinstance(err, BleakError) and any(marker in message for marker in PHANTOM_CONNECTION_MARKERS)


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
    # Time to stay connected after the last write before disconnecting. A write without response
    # is only queued locally when write_gatt_char() returns; dropping the link straight away can
    # discard it before it is sent over the air. 0 keeps the previous behavior.
    settle_delay: float = 0.0
    # Called with the still-connected client after the writes (and settle_delay). None keeps the
    # previous behavior.
    after_write: "Callable[[BleakClientWithServiceCache], Awaitable[None]] | None" = None


def build_v1_command(command: V1Command, pair_id: int) -> bytes:
    """
    Build the 20-byte V1 power command for a station's pair ID (BSID).

    Byte layout matches ShayBox/Lighthouse's v1_command(), which users report switches V1
    stations reliably: the 4-byte command prefix, the BSID little-endian (so "AABBCCDD" is sent
    as DD CC BB AA), then zero padding.
    """
    command_bytes = bytearray(20)
    command_bytes[0:4] = command.value
    command_bytes[4:8] = struct.pack("<I", pair_id)
    return bytes(command_bytes)


V1Readback = Literal["on", "off", "restarted", "unknown"]

# What the read-back shows once the station has received each command.
V1_COMMAND_READBACK: dict[V1Command, V1Readback] = {V1Command.TURN_ON: "on", V1Command.TURN_OFF: "off"}


def decode_v1_readback(value: bytes) -> V1Readback:
    """
    Decode a read of the V1 power characteristic.

    It holds the last command the station received, not its real state (#4). "restarted" means no
    command since the station powered up. Anything else, e.g. a command another app sent with
    different bytes, is "unknown".
    """
    prefix = bytes(value[:4])
    if prefix == V1_READBACK_ON:
        return "on"
    if prefix == V1_READBACK_OFF:
        return "off"
    if prefix == V1_READBACK_RESTARTED and not any(value):
        return "restarted"
    return "unknown"


def v1_pair_id_matches_name(pair_id: int, name: str | None) -> bool | None:
    """
    Check a V1 pair ID against the station's advertised name.

    A V1 station advertises as "HTC BS xxxxxx", ending in the last 4 hex digits of the BSID
    printed on its back. ShayBox/Lighthouse matches stations to BSIDs this way. Returns None when
    the name doesn't look like a V1 name, so there's nothing to check against.
    """
    if not name or not name.startswith(V1_NAME_PREFIX) or len(name) < len(V1_NAME_PREFIX) + 4:
        return None
    return name[-4:].upper() == f"{pair_id:08X}"[-4:]


@dataclass(repr=False)
class _ConnectAttempt:
    """One in-flight establish_connection() call and who still depends on it - see _await_connection()."""

    task: asyncio.Task[BleakClientWithServiceCache]
    started: float
    warned: bool = False
    # Kept per attempt, not per device: a new attempt can start before the previous one's done
    # callback has run, and a shared count would then make that callback think its own client
    # still had a taker.
    waiters: int = 0
    # A waiter returned the connected client to its caller, which now owns closing it.
    claimed: bool = False
    # The client was already handed to _close_abandoned_client().
    closed: bool = False


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
        # Monotonic time the device last became unavailable, None while available. Starts at setup,
        # since _available starts False, so a station that is unreachable from the start gets its
        # outage measured too.
        self._unavailable_since: float | None = time.monotonic()
        self._last_successful_connection = 0.0
        self._current_client: BleakClientWithServiceCache | None = None
        self._client_lock = asyncio.Lock()
        self._last_error_out_of_slots = False
        # Monotonic deadline before which polls don't try to connect - see CONNECT_TIMEOUT_COOLDOWN.
        self._connect_cooldown_until = 0.0
        # Last proxy _penalise_connected_proxy warned about, so it warns once per proxy, not per poll.
        self._last_penalised_proxy: str | None = None
        self._penalty_unsupported_warned = False

        # Tracks an in-flight establish_connection() call that outlives our own willingness to
        # wait for it - see _await_connection().
        self._pending_connect: _ConnectAttempt | None = None

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
        attempt = self._pending_connect
        if attempt is None or attempt.task.done():
            return None
        return time.monotonic() - attempt.started

    @property
    def connect_cooldown_remaining(self) -> float | None:
        """Return seconds until polls may connect again after a connect timeout, or None if not cooling down."""
        remaining = self._connect_cooldown_until - time.monotonic()
        return remaining if remaining > 0 else None

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
        """
        Return a copy of the cached device information.

        A copy, like everything read_device_info() returns: handed out as is, it becomes the info
        coordinator's data, which the next read would then change in place.
        """
        return dict(self._info)

    @property
    def has_cached_info(self) -> bool:
        """Return True if device info has been successfully read."""
        return self._device_info_read_success

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info for the registry."""
        info = DeviceInfo(
            identifiers={(DOMAIN, self.mac)},
            connections={(CONNECTION_BLUETOOTH, self.mac)},
            name=self.device_name,
            manufacturer="Valve" if self.default_name == "Valve Basestation" else "HTC",
            model=self.default_name,
            serial_number=self.mac,
        )
        # Only once read: a None here would overwrite the versions already in the registry on every
        # reload. BasestationInfoCoordinator writes them after each read.
        if firmware := self.get_info("firmware"):
            info["sw_version"] = firmware
        if hardware := self.get_info("hardware"):
            info["hw_version"] = hardware
        return info

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

    def _seen_by_proxies(self, *, connectable: bool = True) -> list[str]:
        """
        Return the names of scanners/proxies currently seeing this device's advertisements.

        Pulled into the "now unavailable" warning so a proxy-wide problem (one proxy crashing or
        losing Wi-Fi and taking every station behind it down at once) is visible directly in the
        log, rather than only discoverable afterwards by downloading diagnostics from every
        affected device and cross-referencing them by hand. connectable=False includes passive
        scanners.
        """
        return [
            scanner_device.scanner.name
            for scanner_device in bluetooth.async_scanner_devices_by_address(
                self.hass, self.mac, connectable=connectable
            )
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
        if (attempt := self._pending_connect) and not attempt.task.done():
            _, pending = await asyncio.wait({attempt.task}, timeout=5.0)
            if pending:
                _LOGGER.debug("Pending connect for %s did not finish during cleanup", self.mac)

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
            downtime = time.monotonic() - self._unavailable_since if self._unavailable_since is not None else None
            self._unavailable_since = None
            self.connection_log.record(Phase.AVAILABILITY, Outcome.AVAILABLE, detail=reason, duration=downtime)
            _LOGGER.info(
                "Basestation %s is available again (%s)%s",
                self.mac,
                reason,
                f", after {downtime:.0f}s unavailable" if downtime is not None else "",
            )
            return

        self._unavailable_since = time.monotonic()
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
        self.connection_log.stats.operation_failed += 1
        if self._consecutive_failures >= UNAVAILABLE_AFTER_CONSECUTIVE_FAILURES:
            self._set_available(f"{self._consecutive_failures} consecutive failures", available=False)

    def _update_power_state(self, state: int) -> None:
        self._last_power_state = state
        self._last_power_state_update = time.time()
        self._is_on = state != 0x00

    def _command_failed(self) -> HomeAssistantError:
        """Return the error a user command raises when it never reached the station."""
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"name": self.device_name},
        )

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
        if op.settle_delay:
            await asyncio.sleep(op.settle_delay)
        if op.after_write:
            await op.after_write(client)
        return True

    async def _execute_single_ble_attempt(
        self, op: BLEOperationRead | BLEOperationWrite, attempt: int
    ) -> bool | bytearray | None:
        """Execute a single BLE connection and operation attempt. Returns None on failure."""
        client = None
        connected_path: tuple[Any, str] = (None, self.mac)
        result: bool | bytearray | None = None

        try:
            await connect_delay(attempt)

            device = self.get_ble_device()
            if not device:
                return None

            client = await asyncio.wait_for(self._await_connection(device), timeout=self.connection_timeout)
            connected_path = self._connected_path(client)

            async with client:
                self._current_client = client
                result = await self._perform_ble_operation(client, op)

                self._record_connection_success()
                await self._safe_disconnect(client, "completing operation")

            # Delay to allow BLE Proxy to internally clear the connection slot
            await asyncio.sleep(0.5)

        except Exception as err:
            self._record_connect_exception(err, "contacting")
            self._react_to_attempt_failure(err, client, connected_path)
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
        forever, regardless of how generous the timeout is. So the connect itself is never cancelled:
        giving up here only stops us *waiting* for it, never the attempt itself. If another
        attempt (or the next poll cycle) comes looking for a connection before this one resolves,
        it reuses this same in-flight task instead of racing a second, competing connection to the
        same device. Whatever happens if nobody ends up waiting for it is handled by
        _on_connect_task_done/_close_if_unclaimed.
        """
        attempt = self._pending_connect
        if attempt is None or attempt.task.done():
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
            attempt = _ConnectAttempt(task=task, started=time.monotonic())
            task.add_done_callback(partial(self._on_connect_task_done, attempt))
            self._pending_connect = attempt
            self.connection_log.stats.connect_started += 1
        else:
            self.connection_log.stats.connect_reused += 1
            self._warn_if_connect_is_stuck(attempt)

        task = attempt.task
        attempt.waiters += 1
        try:
            # asyncio.wait() never cancels the task when we are cancelled, same as shield(), but
            # unlike shield() on Python 3.14.6+ it doesn't log the task's eventual exception as an
            # ERROR ("... exception in shielded future") once our wait has been abandoned. Waiters
            # still here get the exception from task.result(); if none are left,
            # _on_connect_task_done records it.
            await asyncio.wait({task})
            client = task.result()
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
                    duration=time.monotonic() - attempt.started,
                    detail=f"{attempt.waiters - 1} other waiter(s) remaining",
                )
            raise
        except Exception as err:
            self.connection_log.stats.connect_failed += 1
            self.connection_log.record_exception(
                Phase.CONNECT, Outcome.ERROR, err, duration=time.monotonic() - attempt.started
            )
            raise
        else:
            attempt.claimed = True
            self.connection_log.stats.connect_succeeded += 1
            self.connection_log.record(
                Phase.CONNECT, Outcome.OK, duration=time.monotonic() - attempt.started, store=False
            )
            return client
        finally:
            attempt.waiters -= 1
            # The connect may have finished in the same loop iteration our wait was cancelled in.
            # Its done callback then already ran, found us still waiting and left the client to us.
            self._close_if_unclaimed(attempt)

    def _warn_if_connect_is_stuck(self, attempt: _ConnectAttempt) -> None:
        """Warn once when an in-flight connect has been running implausibly long."""
        age = time.monotonic() - attempt.started
        if age < PENDING_CONNECT_WARN_AGE or attempt.warned:
            return

        attempt.warned = True
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

    def _on_connect_task_done(self, attempt: _ConnectAttempt, task: asyncio.Task[BleakClientWithServiceCache]) -> None:
        """
        Handle a connect attempt finishing, closing its client if nobody is waiting for it any more.

        If it failed, establish_connection() already ran its own cleanup before raising, so there's
        nothing left to do but record it. If waiters remain, they see the outcome themselves, and
        whichever leaves last without taking the client closes it (see _close_if_unclaimed).
        """
        if self._pending_connect is attempt:
            self._pending_connect = None

        if attempt.waiters > 0:
            return

        age = time.monotonic() - attempt.started
        if task.cancelled():
            self.connection_log.record(Phase.CONNECT, Outcome.CANCELLED, duration=age, detail="nobody waiting")
            return
        if (err := task.exception()) is not None:
            # establish_connection() ran its own cleanup before raising, so the slot is not at
            # risk here - but the error is still worth keeping, because this is the one connect
            # outcome nobody is awaiting and it would otherwise vanish entirely.
            self.connection_log.record_exception(
                Phase.CONNECT, Outcome.ERROR, err, duration=age, context="nobody waiting"
            )
            return

        self._close_if_unclaimed(attempt)

    def _close_if_unclaimed(self, attempt: _ConnectAttempt) -> None:
        """
        Close the client of a connect that succeeded once nothing was waiting for it any more.

        Called from both the done callback and each waiter on its way out, because either can be
        the last to look: exactly one of them gets here with the task finished, no waiters left and
        the client untaken, and `closed` keeps a late second call from closing it again.
        """
        task = attempt.task
        if attempt.waiters or attempt.claimed or attempt.closed or not task.done() or task.cancelled():
            return
        if task.exception() is not None:
            return

        attempt.closed = True
        _LOGGER.info(
            "Connect for basestation %s succeeded after %.0fs, once nothing was waiting for it any more; "
            "disconnecting it so it cannot hold a proxy connection slot",
            self.mac,
            time.monotonic() - attempt.started,
        )
        self.hass.async_create_background_task(
            self._close_abandoned_client(task.result()), name=f"basestation_close_abandoned_{self.mac}"
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
        (which _await_connection never cancels), a disconnect running when the coordinator's overall
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
        # A user command queued behind a poll has to outlast it, and a poll holds the lock for up to
        # two attempts of connection_timeout each (a timed-out connect starts the cooldown, which
        # ends the poll's retries). A fixed wait silently dropped commands at larger timeouts.
        lock_timeout = 2 * self.connection_timeout + 5.0 if isinstance(op, BLEOperationWrite) else 10.0
        holder = f"{'write' if isinstance(op, BLEOperationWrite) else 'read'} {op.characteristic_uuid}"
        # Reads are the periodic state poll; writes are user commands, which always get to try.
        is_poll = isinstance(op, BLEOperationRead)

        if not await self._acquire_lock(lock_timeout, holder):
            return False

        try:
            # Checked only once the lock is held: a poll that queued behind the attempt that
            # started the cooldown must not go straight on to make another one.
            if is_poll and self._skip_poll_for_cooldown("state read"):
                return False

            self._last_connection_attempt = time.time()
            max_attempts = MAX_RETRIES if op.retry else 1

            for attempt in range(max_attempts):
                result = await self._execute_single_ble_attempt(op, attempt)
                if result is not None:
                    return result

                if is_poll and self.connect_cooldown_remaining is not None:
                    break

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

    def _react_to_attempt_failure(
        self, err: Exception, client: BleakClientWithServiceCache | None, connected_path: tuple[Any, str]
    ) -> None:
        """
        Act on the two failure shapes that leave a proxy in a state the next attempt makes worse.

        `client` is None when the connect itself failed, and set when it connected but the
        operation on it then failed. `connected_path` is what _connected_path() captured right
        after that connect.
        """
        if client is None:
            if _is_connect_timeout_error(err):
                self._start_connect_cooldown()
        elif _is_phantom_connection_error(err):
            self._penalise_connected_proxy(*connected_path)

    def _start_connect_cooldown(self) -> None:
        self._connect_cooldown_until = time.monotonic() + CONNECT_TIMEOUT_COOLDOWN
        _LOGGER.debug(
            "Connect to %s timed out; polls will not try to connect again for %.0fs",
            self.mac,
            CONNECT_TIMEOUT_COOLDOWN,
        )

    def _skip_poll_for_cooldown(self, what: str) -> bool:
        """Return True (and record it) if a poll should not connect right now - see CONNECT_TIMEOUT_COOLDOWN."""
        if (remaining := self.connect_cooldown_remaining) is None:
            return False
        self.connection_log.stats.connect_skipped_cooldown += 1
        self.connection_log.record(
            Phase.CONNECT,
            Outcome.SKIPPED,
            detail=f"{what} skipped after a connect timeout",
        )
        _LOGGER.debug("Skipping %s for %s, %.0fs of connect cooldown left", what, self.mac, remaining)
        return True

    def _connected_path(self, client: BleakClientWithServiceCache) -> tuple[Any, str]:
        """
        Return the proxy (habluetooth scanner) a fresh connection went through, and the address it used.

        This has to be captured straight after the connect, while the link is still up: from
        habluetooth 7 on (Home Assistant 2026.10+), HaBleakClientWrapper.disconnect() clears
        _connected_scanner/_connected_device, and `async with client` has already disconnected
        by the time a failed operation reaches an except block. Reading them there instead is
        what made the phantom-connection penalty a silent no-op on 2026.10. Private habluetooth
        internals, hence getattr; the scanner is None if they are missing.
        """
        scanner = getattr(client, "_connected_scanner", None)
        connected_device = getattr(client, "_connected_device", None)
        return scanner, getattr(connected_device, "address", None) or self.mac

    def _penalise_connected_proxy(self, scanner: Any, address: str) -> None:
        """
        Make Home Assistant route this station's next connects through a different proxy.

        Home Assistant's Bluetooth wrapper (habluetooth's HaBleakClientWrapper) picks the proxy
        itself on every connect, ignoring which BLEDevice we pass in. It scores each path by RSSI
        minus a penalty per recorded connect failure (BaseHaScanner._score_connection_paths as of
        habluetooth 6.1.0). A proxy that fakes a connection reports it as a *success*, which
        clears its failure count, so it keeps winning and every poll fails the same way. Charging
        it with failures by hand is the only lever available from here.

        This writes the scanner's failure dict directly rather than calling its
        _add_connect_failure() helper: Home Assistant ships habluetooth compiled with Cython, where
        that helper is a cdef method invisible from Python (the first version of this called it,
        and silently did nothing in production), while _connect_failures is declared
        `cdef public dict` and so stays reachable in both the compiled and pure-Python builds.
        These are still private habluetooth internals, so every lookup tolerates them being gone;
        that is logged as a warning once, because a penalty that quietly does nothing is exactly
        how the first version went unnoticed (and, under habluetooth 7, how the second one did: see
        _connected_path). The penalty lasts until that proxy next connects to this station
        successfully.
        """
        connect_failures = getattr(scanner, "_connect_failures", None)
        if not isinstance(connect_failures, dict):
            reason = (
                "no proxy was recorded for the connection"
                if scanner is None
                else f"habluetooth's internals are not what this integration expects ({type(scanner).__name__})"
            )
            log = _LOGGER.warning if not self._penalty_unsupported_warned else _LOGGER.debug
            log("Cannot steer basestation %s away from a Bluetooth proxy that faked a connection: %s", self.mac, reason)
            self._penalty_unsupported_warned = True
            return

        connect_failures[address] = connect_failures.get(address, 0) + PHANTOM_CONNECTION_PENALTY

        proxy_name = getattr(scanner, "name", "unknown proxy")
        self.connection_log.stats.proxy_penalised += 1
        self.connection_log.record(Phase.CONNECT, Outcome.PROXY_PENALISED, detail=proxy_name)
        # Once per proxy in a row, not per poll: if no other proxy can reach the station this
        # repeats every cycle, and the counter already tracks how often.
        log = _LOGGER.warning if proxy_name != self._last_penalised_proxy else _LOGGER.debug
        log(
            "Bluetooth proxy %s reported a connection to basestation %s that was not really there "
            "(%d time(s) so far); steering the next connects to another proxy. If this keeps "
            "happening, restart %s",
            proxy_name,
            self.mac,
            self.connection_log.stats.proxy_penalised,
            proxy_name,
        )
        self._last_penalised_proxy = proxy_name

    async def _read_standard_characteristics(
        self, client: BleakClientWithServiceCache, info: dict[BaseStationDeviceInfoKey, str]
    ) -> bool:
        any_read_successful = False
        first_read = True

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
                    # V1 pads its model string with NUL bytes ("2.11\0\0\0\0"), which strip() keeps.
                    info[key] = _value.decode("utf-8").replace("\x00", "").strip()
                    any_read_successful = True
            except BleakCharacteristicNotFoundError:
                # Looked up in the cached services without going over the link (V1 has no firmware
                # characteristic), so the next read is still the first one.
                _LOGGER.debug("Basestation %s has no %s characteristic", self.mac, key)
                continue
            except BleakError as err:
                # The first read on this connection (these run before _read_specific_info) failing
                # this way: a proxy faking the connection. Fail the attempt so the proxy is
                # penalised, as for a state poll (see _react_to_attempt_failure). Later in the read
                # it is a real link that dropped, which keeps what was read.
                if first_read and _is_phantom_connection_error(err):
                    raise
                _LOGGER.debug("BLE error reading characteristic %s: %s", key, err)
            except TimeoutError as err:
                _LOGGER.debug("Timeout reading characteristic %s: %s", key, err)
            except Exception:
                _LOGGER.exception("Unexpected error reading characteristic %s", key)
            first_read = False

        return any_read_successful

    async def _attempt_device_info_read(self) -> dict[BaseStationDeviceInfoKey, str] | None:
        device = self.get_ble_device()
        if not device:
            return None

        info: dict[BaseStationDeviceInfoKey, str] = {}
        client = None
        connected_path: tuple[Any, str] = (None, self.mac)
        std_success = False
        spec_success = False

        try:
            client = await asyncio.wait_for(self._await_connection(device), timeout=self.connection_timeout)
            connected_path = self._connected_path(client)

            async with client:
                self._current_client = client

                std_success = await self._read_standard_characteristics(client, info)
                spec_success = await self._read_specific_info(client, info)

                if std_success or spec_success:
                    await self._safe_disconnect(client, "completing device info read")
                    await asyncio.sleep(0.5)

        except Exception as err:
            self._record_connect_exception(err, "reading device info for")
            self._react_to_attempt_failure(err, client, connected_path)
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
            return dict(self._info)

        lock_timeout = 15.0 if not self._device_info_read_success else 5.0

        if not await self._acquire_lock(lock_timeout, "device info read"):
            return dict(self._info)

        refresh_failed = False
        try:
            if self._skip_poll_for_cooldown("device info read"):
                return dict(self._info)

            for attempt in range(INFO_READ_RETRIES):
                if attempt > 0:
                    if self.connect_cooldown_remaining is not None:
                        break

                    delay = (
                        BLEAK_OUT_OF_SLOTS_BACKOFF_TIME
                        if self._last_error_out_of_slots
                        else CONNECTION_DELAY * (2**attempt)
                    )
                    await asyncio.sleep(delay)

                self._last_connection_attempt = time.time()
                info = await self._attempt_device_info_read()

                if info:
                    refresh_failed = False
                    self._info |= info
                    self._record_connection_success()
                    self._last_device_info_read = current_time
                    self._device_info_read_success = True
                    # The merged view, not just this pass: a read where some characteristics failed
                    # would otherwise blank their sensors until the next full read.
                    return dict(self._info)
                refresh_failed = True

        finally:
            # One failure per refresh, like a state poll. Counting each attempt let a single failed
            # refresh reach UNAVAILABLE_AFTER_CONSECUTIVE_FAILURES on its own. Here rather than after
            # the loop, so a refresh the coordinator's timeout cancels after a failed attempt counts too.
            if refresh_failed:
                self._record_connection_failure()
            self._release_lock()

        return dict(self._info)

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
        await self._write_power_state(BasestationPowerState.STARTING_UP)

    async def turn_off(self) -> None:
        """Turn off the device."""
        await self._write_power_state(BasestationPowerState.SLEEP)

    async def _write_power_state(self, state: BasestationPowerState) -> None:
        """
        Send a power state, showing it right away until a poll confirms or the write fails.

        Deliberately no early return when the station already looks to be in `state`: that value
        is up to a poll interval old (longer during a connect cooldown), and SteamVR may have
        switched the station since, so trusting it drops real commands. Sending a state the station
        is already in is harmless.
        """
        previous = (self._last_power_state, self._last_power_state_update, self._is_on)
        self._target_power_state = state
        self._target_state_expires = time.time() + 15.0
        self._update_power_state(state)
        shown_at = self._last_power_state_update

        if await self.async_ble_operation(
            BLEOperationWrite(
                V2_PWR_CHARACTERISTIC,
                bytes([state]),
                without_response=True,
                repeat_count=3,
                repeat_delay=1.0,
            )
        ):
            return

        # Take the optimistic state back, or the switch would show a state the station was never
        # told about and the target window would keep the next polls from correcting it. Unless
        # something newer (a later command, a confirming read) has replaced it in the meantime.
        if self._last_power_state_update == shown_at:
            self._target_power_state = None
            self._last_power_state, self._last_power_state_update, self._is_on = previous
        raise self._command_failed()

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
                active_states = BOOTING_STATES | {BasestationPowerState.ON}

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
        await self._write_power_state(BasestationPowerState.STANDBY)

    async def identify(self) -> None:
        """Make the device blink its LED to identify it."""
        if not await self.async_ble_operation(
            BLEOperationWrite(
                V2_IDENTIFY_CHARACTERISTIC,
                V2Command.IDENTIFY.value,
                without_response=True,
                repeat_count=3,
                repeat_delay=1.0,
            )
        ):
            raise self._command_failed()

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
        self._pair_id_checked = False
        self._passive_only_warned = False
        # GATT layout seen on the last info read, for diagnostics. It shows straight away whether a
        # station that won't switch exposes the power characteristic at all.
        self.gatt_services: list[dict[str, Any]] | None = None
        # For diagnostics: the last read of the power characteristic, the last few reads that changed
        # it, and how the last command's read-back went.
        self.last_readback: dict[str, Any] | None = None
        self.readback_changes: deque[dict[str, Any]] = deque(maxlen=5)
        self.last_command_result: dict[str, Any] | None = None

    @property
    def default_name(self) -> str:
        """Return the default name."""
        return "Vive Basestation"

    @property
    def advertised_name(self) -> str | None:
        """Return the station's name from HA's last advertisement of it, passive scanners included."""
        service_info = bluetooth.async_last_service_info(self.hass, self.mac, connectable=False)
        return service_info.name if service_info else None

    @property
    def pair_id_matches_name(self) -> bool | None:
        """Return whether the pair ID fits the advertised name (see v1_pair_id_matches_name), None if unknown."""
        return v1_pair_id_matches_name(self.pair_id, self.advertised_name) if self.pair_id else None

    @property
    def last_command_confirmed(self) -> bool:
        """Return True if the last command was read back from the station, which also set the state."""
        return self.last_command_result is not None and self.last_command_result["result"] == "confirmed"

    def restore_is_on(self, *, is_on: bool) -> None:
        """
        Restore the last known on/off state after a restart, until the first poll reads it back.

        Better than the fresh-object default of False, which would show a station that's on as off
        until then. The entity is added after the first poll, so skip it if that poll already read
        a state: the restored one is older.
        """
        if self.last_readback is not None and self.last_readback["meaning"] != "unknown":
            return
        self._is_on = is_on

    async def turn_on(self) -> None:
        """Turn on the device."""
        # Deliberately no early return when _is_on already looks True: it's up to a poll interval
        # old, and SteamVR or another app may have switched the station since. Sending a command
        # the station is already in is harmless.
        await self._send_power_command(V1Command.TURN_ON)

    async def turn_off(self) -> None:
        """Turn off the device."""
        # See turn_on() for why this doesn't early-return based on _is_on.
        await self._send_power_command(V1Command.TURN_OFF)

    async def _send_power_command(self, command: V1Command) -> None:
        """
        Send a V1 power command and check that it arrived by reading it back.

        Raises if it never reached the station, or if the station still reports a different last
        command after V1_COMMAND_ATTEMPTS writes. The read-back only shows that the station received
        the command: whether one with the wrong pair ID changes it is unknown, which is what
        _check_pair_id_against_name() is for.
        """
        if not self.pair_id:
            _LOGGER.warning("Cannot send %s to V1 basestation %s: no pair ID configured", command.name, self.mac)
            return

        try:
            payload = build_v1_command(command, int(self.pair_id))
        except (ValueError, struct.error):
            _LOGGER.exception("Invalid pair_id format for V1 basestation %s", self.mac)
            return

        outcome: dict[str, Any] = {}
        sent = await self.async_ble_operation(
            BLEOperationWrite(
                V1_PWR_CHARACTERISTIC,
                payload,
                without_response=True,
                settle_delay=V1_SETTLE_DELAY,
                after_write=lambda client: self._verify_power_command(client, command, payload, outcome),
            )
        )
        _LOGGER.debug(
            "Sent %s to V1 basestation %s: %s", command.name, self.mac, outcome.get("result") if sent else "failed"
        )
        if not sent:
            raise self._command_failed()

        if outcome.get("result") == "mismatch":
            _LOGGER.warning(
                "V1 basestation %s did not take %s: after %d writes it still reports '%s' as its last command",
                self.mac,
                command.name,
                outcome["writes"],
                outcome["readback"],
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_not_taken",
                translation_placeholders={"name": self.device_name, "attempts": str(outcome["writes"])},
            )

        # Confirmed already set the state from the read-back. Unverified means the write went through
        # but couldn't be read back, so fall back to the command itself, as before the read-back existed.
        if outcome.get("result") != "confirmed":
            self._is_on = command is V1Command.TURN_ON

    async def _verify_power_command(
        self, client: BleakClientWithServiceCache, command: V1Command, payload: bytes, outcome: dict[str, Any]
    ) -> None:
        """
        Read a just-sent command back, rewriting it until the station reports it (BLEOperationWrite.after_write).

        Fills in outcome, which belongs to this one command: another one can be waiting for the lock
        meanwhile. last_command_result gets a copy for diagnostics. A read that fails stops it as
        "unverified" rather than failing the command, since the write itself went through.
        """
        expected = V1_COMMAND_READBACK[command]
        result = "unverified"
        readback = None
        writes = 1
        while True:
            try:
                value = bytes(await client.read_gatt_char(V1_PWR_CHARACTERISTIC))
            except Exception as err:
                _LOGGER.debug("Could not read %s back from V1 basestation %s: %s", command.name, self.mac, err)
                result = "unverified"
                break

            readback = self._apply_readback(value, command.name)
            if decode_v1_readback(value) == expected:
                result = "confirmed"
                break
            result = "mismatch"
            if writes >= V1_COMMAND_ATTEMPTS:
                break

            writes += 1
            await client.write_gatt_char(V1_PWR_CHARACTERISTIC, payload, response=False)
            await asyncio.sleep(V1_SETTLE_DELAY)

        outcome |= {
            "at": time.time(),
            "command": command.name,
            "result": result,
            "writes": writes,
            "readback": readback,
        }
        self.last_command_result = dict(outcome)

    def _check_pair_id_against_name(self) -> None:
        """Warn once if the configured pair ID can't belong to this station (see v1_pair_id_matches_name)."""
        if self._pair_id_checked or not self.pair_id:
            return
        # Without a scan response the name is just the address, which says nothing; try again next poll.
        matches = self.pair_id_matches_name
        if matches is None:
            return

        self._pair_id_checked = True
        if not matches:
            _LOGGER.warning(
                "The pair ID configured for V1 basestation %s (ending %s) does not match its Bluetooth "
                "name '%s', which ends in the last 4 characters of its real ID. The station will ignore "
                "on/off commands. Remove and re-add it with the ID printed on its back",
                self.mac,
                f"{self.pair_id:08X}"[-4:],
                self.advertised_name,
            )

    def _warn_if_only_passively_seen(self) -> None:
        """
        Warn once if the station is only heard by scanners that can't connect to it.

        get_ble_device() only returns a device behind a connectable adapter or proxy. A station
        heard only by passive scanners (an ESPHome proxy without `active: true`, a Shelly gateway,
        ...) still shows up in HA's Bluetooth pages, so it looks reachable while no command can
        ever reach it. _set_available() logs nothing here either: the entry starts out
        unavailable, so there is no transition. A tester in #4 hit exactly this with no log output.
        """
        if self._passive_only_warned:
            return
        passive_scanners = self._seen_by_proxies(connectable=False)
        if not passive_scanners:
            return

        self._passive_only_warned = True
        _LOGGER.warning(
            "V1 basestation %s is only seen by Bluetooth scanners that cannot connect to it (%s), so it "
            "cannot be switched. It needs a connectable Bluetooth adapter or an ESPHome Bluetooth proxy "
            "with `active: true` in range",
            self.mac,
            ", ".join(passive_scanners),
        )

    async def update(self) -> None:
        """Read the last command the station received, the closest V1 has to a state (see decode_v1_readback)."""
        self._check_pair_id_against_name()

        ble_device = self.get_ble_device()
        if ble_device is None:
            # As in ValveBasestationDevice.update(): availability otherwise comes from the read.
            self._warn_if_only_passively_seen()
            self._set_available("no longer advertising to any Bluetooth adapter or proxy", available=False)
            return

        value = await self.async_ble_operation(BLEOperationRead(V1_PWR_CHARACTERISTIC))
        if value:
            self._apply_readback(bytes(value), "poll")

    def _apply_readback(self, value: bytes, source: str) -> str:
        """
        Take a read of the power characteristic as the station's state, returning it as hex.

        "restarted" counts as on: a V1 station comes up on when it gets power (assumed, not confirmed
        in #4). An unknown value, e.g. a command another app sent with different bytes, leaves the
        state alone. source is "poll" or the command that was just sent.
        """
        meaning = decode_v1_readback(value)
        hex_bytes = [f"{b:02x}" for b in value]
        # Diagnostics get posted publicly. The read-back has never contained the pair ID, but mask it
        # in case some firmware echoes the whole command.
        if self.pair_id and value[4:8] == struct.pack("<I", self.pair_id):
            hex_bytes[4:8] = ["xx"] * 4
        hex_value = " ".join(hex_bytes)
        entry = {"at": time.time(), "source": source, "value": hex_value, "meaning": meaning}
        changed = self.last_readback is None or self.last_readback["value"] != hex_value
        self.last_readback = entry
        if changed:
            self.readback_changes.append(entry)
            _LOGGER.debug(
                "V1 basestation %s power characteristic (%s): %s (%s)", self.mac, source, entry["value"], meaning
            )

        if meaning != "unknown":
            is_on = meaning != "off"
            if source == "poll" and is_on != self._is_on:
                _LOGGER.debug(
                    "V1 basestation %s %s outside Home Assistant",
                    self.mac,
                    "restarted" if meaning == "restarted" else f"was switched {meaning}",
                )
            self._is_on = is_on
        return hex_value

    async def _read_specific_info(
        self, client: BleakClientWithServiceCache, info: dict[BaseStationDeviceInfoKey, Any]
    ) -> bool:
        self._record_gatt_services(client)
        if self.pair_id:
            info["pair_id"] = f"0x{self.pair_id:08X}"
        # Nothing here came from the station (the pair ID is from the config entry), so it can't
        # make the read count as a success when every characteristic read failed.
        return False

    def _record_gatt_services(self, client: BleakClientWithServiceCache) -> None:
        """Keep the station's GATT layout for diagnostics, warning once if the power characteristic is missing."""
        try:
            services: list[dict[str, Any]] = [
                {
                    "uuid": service.uuid,
                    "characteristics": [
                        {"uuid": char.uuid, "properties": list(char.properties)} for char in service.characteristics
                    ],
                }
                for service in client.services
            ]
        except Exception as err:
            _LOGGER.debug("Could not list GATT services of V1 basestation %s: %s", self.mac, err)
            return

        first_capture = self.gatt_services is None
        self.gatt_services = services
        has_power_char = any(
            char["uuid"].lower() == V1_PWR_CHARACTERISTIC for service in services for char in service["characteristics"]
        )
        if first_capture and not has_power_char:
            _LOGGER.warning(
                "V1 basestation %s does not expose the power characteristic %s; on/off commands cannot work. "
                "Please attach this device's diagnostics to "
                "https://github.com/MineTech13/homeassistant-basestation/issues/4",
                self.mac,
                V1_PWR_CHARACTERISTIC,
            )


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
