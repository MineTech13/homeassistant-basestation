"""Sensor component for basestation integration."""

import datetime
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    V2_STATE_DESCRIPTIONS,
)
from .coordinator import BasestationCoordinator, BasestationInfoCoordinator
from .device import BasestationDevice, ValveBasestationDevice, ViveBasestationDevice

if TYPE_CHECKING:
    from collections.abc import Callable

    from .device import BaseStationDeviceInfoKey

_LOGGER = logging.getLogger(__name__)

# Home Assistant rejects a state longer than this outright, and an error message is the one
# diagnostic value here that has no natural length limit.
MAX_STATE_LENGTH = 255

SENSOR_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "firmware": ("Firmware", "mdi:developer-board"),
    "model": ("Model", "mdi:card-text"),
    "hardware": ("Hardware", "mdi:chip"),
    "manufacturer": ("Manufacturer", "mdi:factory"),
    "channel": ("Channel", "mdi:radio-tower"),
    "pair_id": ("Pair ID", "mdi:key-variant"),
}


@dataclass(frozen=True, kw_only=True)
class DiagnosticSensorSpec:
    """Describes one connection-health sensor derived from the device's connection log."""

    key: str
    icon: str
    value_fn: "Callable[[BasestationDevice], StateType | datetime.datetime]"
    state_class: SensorStateClass | None = None
    device_class: SensorDeviceClass | None = None


def _last_error(device: BasestationDevice) -> str | None:
    """Return the most recent failure detail, trimmed to something Home Assistant will accept."""
    failure = device.connection_log.last_failure
    if failure is None or failure.detail is None:
        return None
    return failure.detail[:MAX_STATE_LENGTH]


def _last_success(device: BasestationDevice) -> datetime.datetime | None:
    """Return when the device was last reached successfully."""
    if (last := device.last_successful_connection) is None:
        return None
    return dt_util.utc_from_timestamp(last)


# Disabled by default: these are for diagnosing a problem, not for everyday use, and enabling them
# by default would add six entities per basestation for every user. Turning them on beforehand is
# what makes a *later* failure explainable, though - unlike the log, the recorder keeps their
# history for days, so an enabled counter shows exactly when a station started degrading even if
# nobody noticed until long afterwards.
DIAGNOSTIC_SENSORS: tuple[DiagnosticSensorSpec, ...] = (
    DiagnosticSensorSpec(
        key="connection_failures",
        icon="mdi:bluetooth-off",
        value_fn=lambda device: device.connection_log.stats.failures_total,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    DiagnosticSensorSpec(
        key="abandoned_connects",
        icon="mdi:bluetooth-transfer",
        value_fn=lambda device: device.connection_log.stats.connect_abandoned,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    DiagnosticSensorSpec(
        key="stranded_slots",
        icon="mdi:alert-octagon-outline",
        value_fn=lambda device: device.connection_log.stats.suspected_stranded_slots,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    DiagnosticSensorSpec(
        key="out_of_slots_errors",
        icon="mdi:bluetooth-audio",
        value_fn=lambda device: device.connection_log.stats.out_of_slots,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    DiagnosticSensorSpec(
        key="last_connection_error",
        icon="mdi:alert-circle-outline",
        value_fn=_last_error,
    ),
    DiagnosticSensorSpec(
        key="last_successful_connection",
        icon="mdi:bluetooth-connect",
        value_fn=_last_success,
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the basestation sensors."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data is None:
        return
    device: BasestationDevice = data["device"]
    coordinator: BasestationCoordinator = data["coordinator"]
    info_coordinator: BasestationInfoCoordinator = data["info_coordinator"]

    entities: list[SensorEntity] = []

    # Info Sensors (Static, slow polling via info_coordinator)
    entities.extend(
        [
            BasestationInfoSensor(info_coordinator, device, "firmware", EntityCategory.DIAGNOSTIC),
            BasestationInfoSensor(info_coordinator, device, "model", EntityCategory.DIAGNOSTIC),
            BasestationInfoSensor(info_coordinator, device, "hardware", EntityCategory.DIAGNOSTIC),
            BasestationInfoSensor(info_coordinator, device, "manufacturer", EntityCategory.DIAGNOSTIC),
        ]
    )

    if isinstance(device, ValveBasestationDevice):
        entities.append(BasestationInfoSensor(info_coordinator, device, "channel"))
    elif isinstance(device, ViveBasestationDevice) and device.pair_id:
        entities.append(BasestationInfoSensor(info_coordinator, device, "pair_id", EntityCategory.DIAGNOSTIC))

    # Power State Sensor (Fast polling via Coordinator)
    if isinstance(device, ValveBasestationDevice):
        entities.append(BasestationPowerStateSensor(coordinator, device))

    # Connection health sensors, refreshed by the state coordinator so they track every poll
    entities.extend(BasestationDiagnosticSensor(coordinator, device, spec) for spec in DIAGNOSTIC_SENSORS)

    async_add_entities(entities)


class BasestationInfoSensor(CoordinatorEntity, SensorEntity):
    """Sensor for static basestation information using the InfoCoordinator."""

    def __init__(
        self,
        coordinator: BasestationInfoCoordinator,
        device: BasestationDevice,
        key: "BaseStationDeviceInfoKey",
        entity_category: EntityCategory | None = None,
    ) -> None:
        """Initialize the info sensor."""
        super().__init__(coordinator)
        self._device = device
        self._key: BaseStationDeviceInfoKey = key
        self._attr_unique_id = f"basestation_{device.mac}_{key}"
        self._attr_has_entity_name = True
        self._attr_translation_key = key
        _, icon = SENSOR_DESCRIPTIONS.get(key, (key.capitalize(), "mdi:information"))
        self._attr_icon = icon
        self._attr_entity_category = entity_category
        self._attr_device_info = device.device_info

    @property
    def native_value(self) -> str | None:
        """Return the state based on info coordinator data."""
        if not self.coordinator.data:
            return self._device.get_info(self._key, STATE_UNKNOWN)
        return self.coordinator.data.get(self._key, STATE_UNKNOWN)


class BasestationDiagnosticSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor exposing BLE connection health, so problems stay visible after the logs are gone.

    Availability is deliberately not tied to the device being reachable: a counter that goes
    unavailable exactly when the station does would hide the reason it went unavailable, which is
    the only thing these sensors exist to show.
    """

    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: BasestationCoordinator,
        device: BasestationDevice,
        spec: DiagnosticSensorSpec,
    ) -> None:
        """Initialize the diagnostic sensor."""
        super().__init__(coordinator)
        self._device = device
        self._spec = spec
        self._attr_unique_id = f"basestation_{device.mac}_{spec.key}"
        self._attr_has_entity_name = True
        self._attr_translation_key = spec.key
        self._attr_icon = spec.icon
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_state_class = spec.state_class
        self._attr_device_class = spec.device_class
        self._attr_device_info = device.device_info

    @property
    def available(self) -> bool:
        """Return True whenever the integration is loaded, regardless of the device's own state."""
        return True

    @property
    def native_value(self) -> StateType | datetime.datetime:
        """Return the current value of this diagnostic."""
        return self._spec.value_fn(self._device)


class BasestationPowerStateSensor(CoordinatorEntity, SensorEntity):
    """Sensor for basestation power state using the DataUpdateCoordinator."""

    def __init__(self, coordinator: BasestationCoordinator, device: BasestationDevice) -> None:
        """Initialize the power state sensor."""
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"basestation_{device.mac}_power_state"
        self._attr_has_entity_name = True
        self._attr_translation_key = "power_state"
        self._attr_icon = "mdi:power-settings"
        self._attr_device_info = device.device_info

    @property
    def native_value(self) -> str:
        """Return the state based on coordinator data."""
        val = self._device.last_power_state
        if val is None:
            return STATE_UNKNOWN
        return V2_STATE_DESCRIPTIONS.get(val, f"Unknown ({hex(val) if isinstance(val, int) else val})")
