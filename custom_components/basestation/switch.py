"""The basestation switch component for Valve Index Base Stations."""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Set

from bleak import BleakClient, BleakError
from homeassistant.components import bluetooth
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    PWR_CHARACTERISTIC,
    PWR_ON,
    PWR_STANDBY,
    CONF_SETUP_METHOD,
    SETUP_AUTOMATIC,
    SETUP_IMPORT,
)

_LOGGER = logging.getLogger(__name__)

# Limit concurrent connections to prevent overwhelming the BLE adapter
CONNECTION_SEMAPHORE = asyncio.Semaphore(2)
CONNECTION_DELAY = 0.5  # Delay between connections in seconds
CONNECTION_TIMEOUT = 10  # Connection timeout in seconds
UPDATE_INTERVAL = 30  # Minimum time between updates in seconds
MAX_RETRIES = 2  # Maximum connection retry attempts

# Track MAC addresses that have been notified
NOTIFIED_MACS = set()

# Legacy platform setup (for migration from YAML config)
def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the sensor platform and migrate to config entries."""
    mac = config.get("mac")
    name = config.get("name")
    
    if not mac:
        _LOGGER.error("MAC address is required for basestation setup")
        return
    
    # Format MAC address consistently for tracking
    formatted_mac = mac.replace(":", "").upper()
    if len(formatted_mac) == 12:
        formatted_mac = ":".join(formatted_mac[i:i+2] for i in range(0, 12, 2))
    
    # Skip entity creation completely and instead create a notification
    if formatted_mac not in NOTIFIED_MACS:
        _LOGGER.info(
            "Detected YAML configuration for basestation '%s' (%s). "
            "Migration will be handled through the UI.", 
            name if name else "Unnamed", 
            formatted_mac
        )
        
        # Create notification data
        notification_data = {
            "message": (
                f"Found Valve Basestation '{name if name else formatted_mac}' configured in YAML.\n\n"
                f"The integration now uses the UI for configuration. "
                f"Please add this device through the UI:\n\n"
                f"1. Go to Settings → Devices & Services\n"
                f"2. Click 'ADD INTEGRATION' and search for 'Valve Index Basestation'\n"
                f"3. Select 'Manual Setup' and enter MAC: {mac}\n\n"
                f"After setting up in the UI, you can safely remove this entry from your configuration.yaml:\n\n"
                f"```yaml\nswitch:\n  - platform: basestation\n    "
                f"mac: '{mac}'\n    name: '{name if name else 'Valve Basestation'}'\n```"
            ),
            "title": "Valve Basestation: Configuration Migration",
            "notification_id": f"basestation_migration_{formatted_mac}"
        }
        
        # Add the notification via service call - this is synchronous and safe
        hass.services.call("persistent_notification", "create", notification_data)
        NOTIFIED_MACS.add(formatted_mac)
    
    # Don't set up any entities from YAML config
    return


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Basestation switch from a config entry."""
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
        _LOGGER.debug("Setting up single configured device: %s", entry.data.get(CONF_NAME))
        entities = [
            BasestationSwitch(
                hass=hass,
                mac=entry.data[CONF_MAC],
                name=entry.data.get(CONF_NAME),
                entry_id=entry.entry_id,
                is_automatic=False,
                is_legacy=(entry.data.get(CONF_SETUP_METHOD) == SETUP_IMPORT)
            )
        ]

    async_add_entities(entities, update_before_add=True)


class BasestationSwitch(SwitchEntity):
    """Representation of a Valve Index Basestation switch."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac: str,
        name: str | None,
        entry_id: str,
        is_automatic: bool,
        is_legacy: bool = False,
    ) -> None:
        """Initialize the switch entity."""
        self.hass = hass
        self._mac = mac
        self._entry_id = entry_id
        self._is_automatic = is_automatic
        self._is_legacy = is_legacy
        self._is_on = False
        self._is_available = False
        self._last_update = None
        self._state_changed = False
        self._retry_count = 0

        # Format MAC address consistently
        self._mac_formatted = mac.replace(":", "").upper()
        if len(self._mac_formatted) == 12:
            self._mac_formatted = ":".join(
                self._mac_formatted[i : i + 2] for i in range(0, 12, 2)
            )

        # Set unique ID for entity
        if is_legacy:
            # For legacy entities imported from YAML, maintain their entity_id format
            # This ensures existing automations continue to work
            self._attr_unique_id = f"basestation_{self._mac_formatted.replace(':', '').lower()}"
        else:
            self._attr_unique_id = f"basestation_{self._mac_formatted}"
        
        # Fix for duplicate naming: Use provided name or generate a default one
        # Avoid using both _attr_name and name() property to prevent duplication
        self._name = name if name else f"Valve Basestation {self._mac_formatted[-5:]}"
        
        # Set entity icon
        self._attr_icon = "mdi:virtual-reality"

        # Set device info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mac_formatted)},
            name=self._name,
            manufacturer="Valve",
            model="Index Basestation",
        )

        # Set parent device if part of automatic discovery
        if self._is_automatic:
            self._attr_device_info["via_device"] = (DOMAIN, f"auto_{entry_id}")

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return self._name

    @property
    def available(self) -> bool:
        """Return the connection status of this switch."""
        return self._is_available

    @property
    def is_on(self) -> bool:
        """Return if the switch is currently on or off."""
        return self._is_on

    async def async_ble_operation(
        self,
        operation_type: str,
        retry: bool = True,
        value: bytes = None,
    ) -> Any:
        """Execute a BLE operation with proper connection management."""
        result = None
        
        for attempt in range(MAX_RETRIES if retry else 1):
            try:
                async with CONNECTION_SEMAPHORE:
                    # Add delay to prevent overwhelming the BLE adapter
                    if attempt > 0:
                        await asyncio.sleep(CONNECTION_DELAY * (2 ** attempt))
                    else:
                        await asyncio.sleep(CONNECTION_DELAY)
                    
                    # Get BLE device from registry
                    device = bluetooth.async_ble_device_from_address(
                        self.hass, self._mac_formatted
                    )
                    if not device:
                        _LOGGER.debug(
                            "Device %s not found in Bluetooth registry", 
                            self._mac_formatted
                        )
                        continue

                    # Connect to device and execute operation
                    async with BleakClient(
                        device, timeout=CONNECTION_TIMEOUT
                    ) as client:
                        if operation_type == "write":
                            await client.write_gatt_char(PWR_CHARACTERISTIC, value)
                            result = True
                        elif operation_type == "read":
                            result = await client.read_gatt_char(PWR_CHARACTERISTIC)
                        
                        # Reset retry count on success
                        self._retry_count = 0
                        return result
                        
            except BleakError as err:
                _LOGGER.debug(
                    "BLE error on basestation '%s' (attempt %d/%d): %s",
                    self._mac_formatted,
                    attempt + 1,
                    MAX_RETRIES if retry else 1,
                    str(err),
                )
                
            except Exception as ex:
                _LOGGER.debug(
                    "Failed to execute %s on basestation '%s': %s",
                    operation_type,
                    self._mac_formatted,
                    str(ex),
                )
                
            # Increment retry count
            self._retry_count += 1
            
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        if await self.async_ble_operation("write", value=PWR_ON):
            self._is_on = True
            self._is_available = True
            self._state_changed = True
            self._last_update = datetime.now()
        else:
            self._is_available = False

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        if await self.async_ble_operation("write", value=PWR_STANDBY):
            self._is_on = False
            self._is_available = True
            self._state_changed = True
            self._last_update = datetime.now()
        else:
            self._is_available = False

    async def async_update(self) -> None:
        """Fetch new state data for the switch."""
        # Skip update if state was just changed manually
        if self._state_changed:
            self._state_changed = False
            return

        # Skip update if last update was recent enough
        now = datetime.now()
        if (
            self._last_update
            and now - self._last_update < timedelta(seconds=UPDATE_INTERVAL)
            and self._is_available
        ):
            return

        # Use less aggressive retry strategy for scheduled updates
        value = await self.async_ble_operation(
            "read", 
            retry=(self._retry_count < MAX_RETRIES),
        )
        
        if value is not False:  # Check if operation was successful
            self._is_on = value != PWR_STANDBY
            self._is_available = True
            self._last_update = now
        else:
            self._is_available = False