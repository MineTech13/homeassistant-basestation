"""The basestation switch."""
import asyncio
import logging
from typing import Any
from datetime import datetime, timedelta

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

# Global connection semaphore to limit concurrent connections
CONNECTION_SEMAPHORE = asyncio.Semaphore(2)  # Limit to 2 concurrent connections
CONNECTION_DELAY = 0.5  # Delay between connections in seconds

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Basestation switch."""
    if entry.data.get(CONF_SETUP_METHOD) == SETUP_AUTOMATIC:
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
        self._last_update = None
        self._state_changed = False

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

    async def _connect_and_execute(self, operation: str, value: bytes = None) -> bool:
        """Connect to device and execute operation with connection management."""
        try:
            async with CONNECTION_SEMAPHORE:
                # Add delay to prevent overwhelming the BLE adapter
                await asyncio.sleep(CONNECTION_DELAY)
                
                device = bluetooth.async_ble_device_from_address(
                    self._hass, str(self._mac)
                )
                if not device:
                    _LOGGER.debug("Device %s not found in bluetooth registry", self._mac)
                    return False

                async with BleakClient(device, timeout=10) as client:
                    if operation == "write":
                        await client.write_gatt_char(PWR_CHARACTERISTIC, value)
                        return True
                    elif operation == "read":
                        value = await client.read_gatt_char(PWR_CHARACTERISTIC)
                        return value
                    
        except Exception as ex:
            _LOGGER.debug(
                "Failed to execute %s on basestation '%s': %s",
                operation,
                self._mac,
                str(ex),
            )
            return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        if await self._connect_and_execute("write", PWR_ON):
            self._is_on = True
            self._is_available = True
            self._state_changed = True
        else:
            self._is_available = False

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        if await self._connect_and_execute("write", PWR_STANDBY):
            self._is_on = False
            self._is_available = True
            self._state_changed = True
        else:
            self._is_available = False

    async def async_update(self) -> None:
        """Fetch new state data for the sensor."""
        # Skip update if state was just changed manually
        if self._state_changed:
            self._state_changed = False
            return

        # Skip update if last update was less than 30 seconds ago
        now = datetime.now()
        if (
            self._last_update
            and now - self._last_update < timedelta(seconds=30)
            and self._is_available
        ):
            return

        value = await self._connect_and_execute("read")
        if value is not False:  # Check if operation was successful
            self._is_on = value != PWR_STANDBY
            self._is_available = True
            self._last_update = now
        else:
            self._is_available = False
