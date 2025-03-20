"""The basestation switch."""
import asyncio
import logging
from typing import Any

from bleak import BleakClient
from homeassistant.components import bluetooth
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    DOMAIN,
    PWR_CHARACTERISTIC,
    PWR_ON,
    PWR_STANDBY,
    CONF_SETUP_METHOD,
    SETUP_AUTOMATIC,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Basestation switch."""
    if entry.data.get(CONF_SETUP_METHOD) == SETUP_AUTOMATIC:
        # Handle automatic configuration
        devices = entry.data.get("devices", [])
        _LOGGER.debug("Setting up %s automatically discovered devices", len(devices))
        entities = [
            BasestationSwitch(
                hass=hass,
                mac=device[CONF_MAC],
                name=device[CONF_NAME],
                entry_id=entry.entry_id,
                is_automatic=True,
            )
            for device in devices
        ]
    else:
        # Handle manual/selection configuration
        _LOGGER.debug("Setting up single manually configured device")
        entities = [
            BasestationSwitch(
                hass=hass,
                mac=entry.data[CONF_MAC],
                name=entry.data.get(CONF_NAME),
                entry_id=entry.entry_id,
                is_automatic=False,
            )
        ]

    async_add_entities(entities, update_before_add=True)


class BasestationSwitch(SwitchEntity):
    """The basestation switch implementation."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac: str,
        name: str | None,
        entry_id: str,
        is_automatic: bool,
    ) -> None:
        """Initialize the switch."""
        self._hass = hass
        self._mac = mac
        self._name = name
        self._entry_id = entry_id
        self._is_automatic = is_automatic
        self._is_on = False
        self._is_available = False

        # Set unique ID
        self._attr_unique_id = f"basestation_{mac}"

        # Set device info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name=name or f"Valve Basestation {mac}",
            manufacturer="Valve",
            model="Index Basestation",
            via_device=(DOMAIN, mac),
        )

        if self._is_automatic:
            # If part of automatic discovery, add to automatic device group
            self._attr_device_info["via_device"] = (DOMAIN, f"auto_{entry_id}")

    @property
    def icon(self) -> str:
        """Return the icon."""
        return "mdi:virtual-reality"

    @property
    def available(self) -> bool:
        """Return the connection status of this switch."""
        return self._is_available

    @property
    def is_on(self) -> bool:
        """Return if the switch is currently on or off."""
        return self._is_on

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return self._name or f"Valve Basestation {self._mac}"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        try:
            device = bluetooth.async_ble_device_from_address(
                self._hass, str(self._mac)
            )
            if not device:
                _LOGGER.error(
                    "Device %s not found in bluetooth registry", self._mac
                )
                self._is_available = False
                return

            async with BleakClient(device, timeout=30) as client:
                await client.write_gatt_char(PWR_CHARACTERISTIC, PWR_ON)
                self._is_on = True
                self._is_available = True
        except Exception as ex:
            self._is_available = False
            _LOGGER.error(
                "Failed to turn on basestation '%s': %s", self._mac, str(ex)
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        try:
            device = bluetooth.async_ble_device_from_address(
                self._hass, str(self._mac)
            )
            if not device:
                _LOGGER.error(
                    "Device %s not found in bluetooth registry", self._mac
                )
                self._is_available = False
                return

            async with BleakClient(device, timeout=30) as client:
                await client.write_gatt_char(PWR_CHARACTERISTIC, PWR_STANDBY)
                self._is_on = False
                self._is_available = True
        except Exception as ex:
            self._is_available = False
            _LOGGER.error(
                "Failed to turn off basestation '%s': %s", self._mac, str(ex)
            )

    async def async_update(self) -> None:
        """Fetch new state data for the sensor."""
        try:
            device = bluetooth.async_ble_device_from_address(
                self._hass, str(self._mac)
            )
            if not device:
                _LOGGER.debug(
                    "Device %s not found in bluetooth registry", self._mac
                )
                self._is_on = False
                self._is_available = False
                return

            async with BleakClient(device, timeout=30) as client:
                value = await client.read_gatt_char(PWR_CHARACTERISTIC)
                self._is_on = value != PWR_STANDBY
                self._is_available = True
        except Exception as ex:
            self._is_on = False
            self._is_available = False
            _LOGGER.debug(
                "Failed to update basestation '%s': %s", self._mac, str(ex)
            )
