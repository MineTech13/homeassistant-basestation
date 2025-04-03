"""The Valve Index Basestation integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from bleak import BleakScanner, BleakError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, CONF_NAME, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import Platform
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    CONF_SETUP_METHOD,
    SETUP_AUTOMATIC,
    CONF_DISCOVERY_PREFIX,
    DISCOVERY_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Define supported platforms
PLATFORMS: list[Platform] = [Platform.SWITCH]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Valve Index Basestation component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Valve Index Basestation from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    # Initialize automatic discovery if configured
    if entry.data.get(CONF_SETUP_METHOD) == SETUP_AUTOMATIC:
        discovery = BasestationDiscovery(hass, entry)
        await discovery.async_start()
        hass.data[DOMAIN][entry.entry_id] = discovery
    else:
        hass.data[DOMAIN][entry.entry_id] = entry.data

    # Set up entities for this entry
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload entities for this config entry
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Clean up discovery instance if automatic setup
        if entry.data.get(CONF_SETUP_METHOD) == SETUP_AUTOMATIC:
            discovery = hass.data[DOMAIN].pop(entry.entry_id)
            await discovery.async_stop()
        else:
            hass.data[DOMAIN].pop(entry.entry_id)
    
    return unload_ok


class BasestationDiscovery:
    """Class to manage automatic discovery of basestations."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the discovery service."""
        self.hass = hass
        self.entry = entry
        self._known_devices = {
            device[CONF_MAC]
            for device in entry.data.get("devices", [])
        }
        self._prefix = entry.data[CONF_DISCOVERY_PREFIX]
        self._remove_interval = None
        self._scanning = False

    async def async_start(self) -> None:
        """Start the discovery process."""
        @callback
        def _async_startup(_):
            """Start discovery when Home Assistant is running."""
            asyncio.create_task(self._async_scan_devices())
            self._remove_interval = async_track_time_interval(
                self.hass,
                self._async_scan_devices,
                timedelta(seconds=DISCOVERY_INTERVAL),
            )

        # Start discovery when Home Assistant is running
        if self.hass.is_running:
            _async_startup(None)
        else:
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _async_startup
            )

    async def async_stop(self) -> None:
        """Stop the discovery process."""
        if self._remove_interval:
            self._remove_interval()
            self._remove_interval = None

    async def _async_scan_devices(self, _=None) -> None:
        """Scan for new devices."""
        # Prevent concurrent scans
        if self._scanning:
            return
            
        self._scanning = True
        
        try:
            _LOGGER.debug("Scanning for new basestation devices...")
            devices = await BleakScanner.discover()
            
            # Filter for new basestation devices
            new_devices = [
                device for device in devices
                if device.address 
                and device.name 
                and device.name.startswith(self._prefix)
                and device.address not in self._known_devices
            ]

            if new_devices:
                _LOGGER.info(
                    "Discovered %d new basestation device(s): %s", 
                    len(new_devices), 
                    ", ".join(f"{device.name} ({device.address})" for device in new_devices)
                )
                
                # Update known devices
                new_data = {
                    **self.entry.data,
                    "devices": [
                        *self.entry.data.get("devices", []),
                        *[{
                            CONF_MAC: device.address,
                            CONF_NAME: device.name,
                        } for device in new_devices]
                    ]
                }
                
                self._known_devices.update(device.address for device in new_devices)
                
                # Update entry data
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data=new_data
                )
                
                # Reload entry to create new entities
                await self.hass.config_entries.async_reload(self.entry.entry_id)
            else:
                _LOGGER.debug("No new basestation devices found")

        except BleakError as err:
            _LOGGER.warning("BLE error during device discovery: %s", err)
        except Exception as ex:
            _LOGGER.error("Error scanning for devices: %s", ex)
        finally:
            self._scanning = False