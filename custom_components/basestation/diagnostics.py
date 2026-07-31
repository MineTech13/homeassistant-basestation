"""
Diagnostics support for the VR Basestation integration.

Downloadable from a basestation's device page in Home Assistant. This exists because the failure
this integration keeps running into - a Bluetooth proxy connection slot getting stuck - is only
noticed hours after it starts, by which point DEBUG logs (if they were even enabled) have rotated
away. Everything needed to reconstruct what happened is therefore kept in memory and dumped here
instead: lifetime counters, a rolling window of connection events, and, crucially, which proxy the
station is actually reachable through right now.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_MAC
from homeassistant.util import dt as dt_util

from .const import CONF_PAIR_ID, DOMAIN
from .device import BasestationDevice, ValveBasestationDevice, ViveBasestationDevice

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

# The MAC identifies the user's hardware and these reports get pasted into GitHub issues. The last
# few characters are kept separately below so a report can still be lined up against log lines,
# which do contain the full address.
TO_REDACT = {CONF_MAC, CONF_PAIR_ID, "mac", "pair_id", "address"}


def _timestamp(value: float | None) -> str | None:
    """Render an epoch value as an ISO timestamp, since raw floats are unreadable in a report."""
    if not value:
        return None
    return dt_util.utc_from_timestamp(value).isoformat()


def _age(value: float | None) -> float | None:
    """Return seconds elapsed since an epoch value, rounded."""
    if not value:
        return None
    return round(time.time() - value, 1)


def _bluetooth_info(hass: HomeAssistant, mac: str) -> dict[str, Any]:
    """
    Describe how the device currently looks to Home Assistant's Bluetooth stack.

    Which proxy sees a station, and how well, is the single most useful thing here: the slot leak
    is a per-proxy problem, so knowing that the one unavailable station is the only one behind a
    particular proxy (or that it dropped to a much weaker scanner) usually explains the failure on
    its own.
    """
    info: dict[str, Any] = {
        "connectable_scanner_count": bluetooth.async_scanner_count(hass, connectable=True),
        "seen_by": [],
    }

    service_info = bluetooth.async_last_service_info(hass, mac, connectable=True)
    if service_info:
        info["last_advertisement"] = {
            "source": service_info.source,
            "rssi": service_info.rssi,
            "name": service_info.name,
            "connectable": service_info.connectable,
            "seconds_ago": round(time.monotonic() - service_info.time, 1),
        }
    else:
        info["last_advertisement"] = None

    for scanner_device in bluetooth.async_scanner_devices_by_address(hass, mac, connectable=True):
        scanner = scanner_device.scanner
        info["seen_by"].append(
            {
                "source": scanner.source,
                "adapter": scanner.adapter,
                "name": scanner.name,
                "rssi": scanner_device.advertisement.rssi if scanner_device.advertisement else None,
            }
        )

    return info


def _device_info(device: BasestationDevice) -> dict[str, Any]:
    """Summarise the device's own view of itself."""
    data: dict[str, Any] = {
        "type": type(device).__name__,
        "mac_suffix": device.mac[-5:],
        "name": device.device_name,
        "available": device.available,
        "is_on": device.is_on,
        "last_power_state": device.last_power_state,
        "last_power_state_age": (round(age, 1) if (age := device.last_power_state_age) is not None else None),
        "consecutive_failures": device.consecutive_failures,
        "last_successful_connection": _timestamp(device.last_successful_connection),
        "seconds_since_successful_connection": _age(device.last_successful_connection),
        "connection_timeout": device.connection_timeout,
        "info_scan_interval": device.info_scan_interval,
        "has_cached_info": device.has_cached_info,
        "cached_info": dict(device.cached_info),
    }

    if isinstance(device, ValveBasestationDevice):
        data["is_in_standby"] = device.is_in_standby
    elif isinstance(device, ViveBasestationDevice):
        data["has_pair_id"] = device.pair_id is not None

    return data


def _live_state(device: BasestationDevice) -> dict[str, Any]:
    """
    Capture what the connection layer is doing at this exact moment.

    Worth grabbing while a station is still stuck: a connect that has been in flight for minutes,
    or a lock held by an operation that never returned, names the culprit directly rather than
    leaving it to be inferred from counters.
    """
    return {
        "connect_in_flight": device.pending_connect_age is not None,
        "connect_in_flight_age": (round(age, 1) if (age := device.pending_connect_age) is not None else None),
        "lock_held_by": device.lock_holder,
        "lock_held_age": round(age, 1) if (age := device.lock_held_age) is not None else None,
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data or not isinstance(device := data.get("device"), BasestationDevice):
        return {"error": "Device is not set up; the config entry may have failed to load"}

    coordinator = data.get("coordinator")
    info_coordinator = data.get("info_coordinator")

    # Absolute timestamps line up with the Home Assistant log; the relative age is what makes the
    # sequence readable at a glance ("everything failed in the last four minutes").
    connection = device.connection_log.as_dict()
    for event in [*connection["events"], connection["last_failure"], connection["last_success"]]:
        if event:
            event["seconds_ago"] = _age(event["at"])
            event["at"] = _timestamp(event["at"])
            if event.get("first_at"):
                event["first_at"] = _timestamp(event["first_at"])

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "device": _device_info(device),
        "live": _live_state(device),
        "connection": connection,
        "bluetooth": _bluetooth_info(hass, device.mac),
        "coordinators": {
            "state": {
                "last_update_success": getattr(coordinator, "last_update_success", None),
                "update_interval": str(getattr(coordinator, "update_interval", None)),
            },
            "info": {
                "last_update_success": getattr(info_coordinator, "last_update_success", None),
                "update_interval": str(getattr(info_coordinator, "update_interval", None)),
            },
        },
    }
