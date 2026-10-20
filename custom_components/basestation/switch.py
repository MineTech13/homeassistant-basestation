"""The basestation switch component."""

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BOOTING_STATES, DOMAIN, BasestationPowerState
from .coordinator import BasestationCoordinator
from .device import BasestationDevice, ValveBasestationDevice, ViveBasestationDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the basestation switch from a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data is None:
        return

    device: BasestationDevice = data["device"]
    coordinator: BasestationCoordinator = data["coordinator"]

    entities: list[SwitchEntity] = [BasestationSwitch(coordinator, device)]

    if isinstance(device, ValveBasestationDevice):
        entities.append(BasestationStandbySwitch(coordinator, device))

    async_add_entities(entities)

    # Shipped in 2.0.1 as plain services taking `entity_id`; as entity services, those calls keep
    # working and targets work too. Registered once per platform, however many entries set up.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service("identify", None, "async_identify")
    platform.async_register_entity_service("set_standby", None, "async_set_standby")


class _BasestationServices(CoordinatorEntity):
    """The basestation.identify and basestation.set_standby entity services, on both switches."""

    _device: BasestationDevice

    async def async_identify(self) -> None:
        """Blink the station's LED."""
        await self._valve_device().identify()

    async def async_set_standby(self) -> None:
        """Put the station into standby."""
        await self._valve_device().set_standby()
        await self.coordinator.async_request_refresh()

    def _valve_device(self) -> ValveBasestationDevice:
        if not isinstance(self._device, ValveBasestationDevice):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="v2_only",
                translation_placeholders={"name": self._device.device_name},
            )
        return self._device


class BasestationSwitch(_BasestationServices, RestoreEntity, SwitchEntity):
    """Representation of a basestation main power switch."""

    def __init__(self, coordinator: BasestationCoordinator, device: BasestationDevice) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"basestation_{device.mac}"
        self._attr_has_entity_name = True
        self._attr_name = None
        self._attr_icon = "mdi:virtual-reality"
        self._attr_device_info = device.device_info

    async def async_added_to_hass(self) -> None:
        """Restore the last known on/off state for V1 devices, which have no way to read it back."""
        await super().async_added_to_hass()

        if not isinstance(self._device, ViveBasestationDevice):
            return

        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in (STATE_ON, STATE_OFF):
            self._device.restore_is_on(is_on=last_state.state == STATE_ON)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return super().available and self._device.available

    @property
    def is_on(self) -> bool:
        """Return if the switch is currently on or off."""
        if isinstance(self._device, ValveBasestationDevice):
            if self._device.last_power_state is None:
                return False

            active_states = BOOTING_STATES | {BasestationPowerState.ON, BasestationPowerState.STANDBY}
            return self._device.last_power_state in active_states

        return self._device.is_on

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn the switch on."""
        await self._device.turn_on()
        await self._refresh_after_command()

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn the switch off."""
        await self._device.turn_off()
        await self._refresh_after_command()

    async def _refresh_after_command(self) -> None:
        # A V1 command that was read back already took the state from the station over the same
        # connection. Polling straight away would only cost the proxy another connect.
        if isinstance(self._device, ViveBasestationDevice) and self._device.last_command_confirmed:
            self.coordinator.async_update_listeners()
        else:
            await self.coordinator.async_request_refresh()


class BasestationStandbySwitch(_BasestationServices, SwitchEntity):
    """Representation of a basestation standby switch (V2 only)."""

    def __init__(self, coordinator: BasestationCoordinator, device: BasestationDevice) -> None:
        """Initialize the standby switch."""
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"basestation_{device.mac}_standby"
        self._attr_has_entity_name = True
        self._attr_translation_key = "standby"
        self._attr_icon = "mdi:sleep"
        self._attr_device_info = device.device_info

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return super().available and self._device.available

    @property
    def is_on(self) -> bool:
        """Return if the standby mode is active."""
        if isinstance(self._device, ValveBasestationDevice):
            return self._device.last_power_state == BasestationPowerState.STANDBY
        return False

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn on standby mode."""
        if isinstance(self._device, ValveBasestationDevice):
            await self._device.set_standby()
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn off standby mode (turn fully on)."""
        if isinstance(self._device, ValveBasestationDevice):
            await self._device.turn_on()
            await self.coordinator.async_request_refresh()
