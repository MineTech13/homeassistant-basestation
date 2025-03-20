"""The basestation switch."""

import asyncio
import logging

from bleak import BleakClient
from homeassistant.components import bluetooth
from homeassistant.components.switch import SwitchEntity

from .const import (
    PWR_CHARACTERISTIC,
    PWR_ON,
    PWR_STANDBY,
)

_LOGGER = logging.getLogger(__name__)


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the sensor platform."""
    mac = config.get("mac")
    name = config.get("name")
    add_entities([BasestationSwitch(hass, mac, name)], update_before_add=True)


class BasestationSwitch(SwitchEntity):
    """The basestation switch implementation."""

    def __init__(self, hass, mac, name):
        """Initialize the switch."""
        self._hass = hass
        self._mac = mac
        self._name = name
        self._is_on = False
        self._is_available = False

    @property
    def icon(self):
        """Return the icon."""
        return "mdi:virtual-reality"

    @property
    def should_poll(self):
        return True

    @property
    def available(self):
        """Return the connection status of this switch."""
        return self._is_available

    @property
    def is_on(self):
        """If the switch is currently on or off."""
        return self._is_on

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        await self._send_command(PWR_ON)

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        await self._send_command(PWR_STANDBY)

    @property
    def name(self):
        """Return the name of the switch."""
        return self._name if self._name else "Valve Basestation"

    async def async_update(self):
        """Fetch new state data for the sensor."""
        try:
            async with BleakClient(self.get_ble_device(), timeout=30) as client:
                self._is_on = await client.read_gatt_char(PWR_CHARACTERISTIC) != PWR_STANDBY
                self._is_available = True
        except asyncio.exceptions.TimeoutError:
            self._is_on = False
            self._is_available = False
            _LOGGER.debug(
                "Timeout occurred when trying to update basestation '%s'.",
                self._mac,
            )
        except Exception as e:
            _LOGGER.error("Unexpected error updating basestation '%s': %s", self._mac, e)

    async def _send_command(self, command):
        """Send a command to the basestation."""
        try:
            async with BleakClient(self.get_ble_device(), timeout=30) as client:
                await client.write_gatt_char(PWR_CHARACTERISTIC, command)
                self._is_available = True
        except asyncio.exceptions.TimeoutError:
            self._is_available = False
            _LOGGER.debug(
                "Timeout occurred when sending command to basestation '%s'.",
                self._mac,
            )
        except Exception as e:
            _LOGGER.error("Unexpected error controlling basestation '%s': %s", self._mac, e)

    def get_ble_device(self):
        return bluetooth.async_ble_device_from_address(self._hass, str(self._mac))
