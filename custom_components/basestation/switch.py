"""The basestation switch component."""

import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_TYPE,
    CONF_PAIR_ID,
    DOMAIN,
    STANDBY_SWITCH_SCAN_INTERVAL,
)
from .device import (
    BasestationDevice,
    ValveBasestationDevice,
    get_basestation_device,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the basestation switch."""
    # Get config entry data
    if entry.data.get("setup_method") == "automatic":
        # For automatic setup, create entities for each discovered device
        devices = entry.data.get("devices", [])
        _LOGGER.debug("Setting up %s automatically discovered devices", len(devices))
        entities: list[SwitchEntity] = []

        for device_data in devices:
            mac = device_data[CONF_MAC]
            name = device_data.get(CONF_NAME)
            device_type = device_data.get(CONF_DEVICE_TYPE, entry.data.get(CONF_DEVICE_TYPE))
            pair_id = device_data.get(CONF_PAIR_ID)

            device = get_basestation_device(hass, mac, name=name, device_type=device_type, pair_id=pair_id)
            if device:
                entities.append(BasestationSwitch(device, entry.entry_id))

                # Add standby switch for Valve basestations
                if isinstance(device, ValveBasestationDevice):
                    entities.append(BasestationStandbySwitch(device, entry.entry_id))

        async_add_entities(entities, update_before_add=True)
    else:
        # For manual or selection setup, create entity for the single device
        mac = entry.data[CONF_MAC]
        name = entry.data.get(CONF_NAME)
        device_type = entry.data.get(CONF_DEVICE_TYPE)
        pair_id = entry.data.get(CONF_PAIR_ID)

        device = get_basestation_device(hass, mac, name=name, device_type=device_type, pair_id=pair_id)
        if device:
            entities = [BasestationSwitch(device, entry.entry_id)]

            # Add standby switch for Valve basestations
            if isinstance(device, ValveBasestationDevice):
                entities.append(BasestationStandbySwitch(device, entry.entry_id))

            async_add_entities(entities, update_before_add=True)


class BasestationSwitch(SwitchEntity):
    """Representation of a basestation switch."""

    def __init__(self, device: BasestationDevice, entry_id: str) -> None:
        """Initialize the switch."""
        self._device = device
        self._entry_id = entry_id
        self._attr_unique_id = f"basestation_{device.mac}"
        self._attr_name = device.device_name
        self._attr_icon = "mdi:virtual-reality"
        self._last_update = 0

        # Create device info for Home Assistant device registry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
            name=device.device_name,
            manufacturer=("Valve" if isinstance(device, ValveBasestationDevice) else "HTC"),
            model=("Index Basestation" if isinstance(device, ValveBasestationDevice) else "Vive Basestation"),
        )

    @property
    def is_on(self) -> bool:
        """Return if the switch is currently on or off."""
        return self._device.is_on

    @property
    def available(self) -> bool:
        """Return if the device is available."""
        return self._device.available

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn the switch on."""
        await self._device.turn_on()
        self.async_write_ha_state()

        # Force update of standby switch
        standby_entity_id = f"switch.{self._device.device_name.lower().replace(' ', '_')}_standby_mode"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": standby_entity_id},
                blocking=False,
            ),
        )

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn the switch off."""
        await self._device.turn_off()
        self.async_write_ha_state()

        # Force update of standby switch
        standby_entity_id = f"switch.{self._device.device_name.lower().replace(' ', '_')}_standby_mode"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": standby_entity_id},
                blocking=False,
            ),
        )

    async def async_update(self) -> None:
        """Fetch new state data for the sensor."""
        await self._device.update()


class BasestationStandbySwitch(SwitchEntity):
    """Representation of a basestation standby switch (V2 only)."""

    def __init__(self, device: BasestationDevice, entry_id: str) -> None:
        """Initialize the switch."""
        self._device = device
        self._entry_id = entry_id
        self._attr_unique_id = f"basestation_{device.mac}_standby"
        self._attr_name = f"{device.device_name} Standby Mode"
        self._attr_icon = "mdi:sleep"
        self._is_in_standby = False
        self._last_update = 0.0

        # Share device info with main switch
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
        )

    @property
    def is_on(self) -> bool:
        """Return if standby mode is active."""
        return self._is_in_standby

    @property
    def available(self) -> bool:
        """Return if the device is available."""
        return self._device.available

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn on standby mode (instead of full sleep)."""
        if isinstance(self._device, ValveBasestationDevice):
            await self._device.set_standby()
            self._is_in_standby = True
            self.async_write_ha_state()

            # Force refresh of power switch
            power_entity_id = f"switch.{self._device.device_name.lower().replace(' ', '_')}"
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "homeassistant",
                    "update_entity",
                    {"entity_id": power_entity_id},
                    blocking=False,
                ),
            )

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn off standby mode (device will go to full on mode)."""
        if isinstance(self._device, ValveBasestationDevice):
            await self._device.turn_on()
            self._is_in_standby = False
            self.async_write_ha_state()

            # Force refresh of power switch
            power_entity_id = f"switch.{self._device.device_name.lower().replace(' ', '_')}"
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "homeassistant",
                    "update_entity",
                    {"entity_id": power_entity_id},
                    blocking=False,
                ),
            )

    async def async_update(self) -> None:
        """Update the standby state based on device state."""
        current_time = time.time()
        if current_time - self._last_update < STANDBY_SWITCH_SCAN_INTERVAL:
            return

        if isinstance(self._device, ValveBasestationDevice):
            # Get the raw power state value to determine if in standby mode
            raw_state = await self._device.get_raw_power_state()

            # Update standby state - 0x02 is the standby state value
            if raw_state == 0x02:  # noqa: PLR2004
                if not self._is_in_standby:
                    self._is_in_standby = True
                    _LOGGER.debug("Standby state changed to ON")
            elif raw_state is not None and self._is_in_standby:  # Only update if we have a valid state
                self._is_in_standby = False
                _LOGGER.debug("Standby state changed to OFF")

            self._last_update = current_time
