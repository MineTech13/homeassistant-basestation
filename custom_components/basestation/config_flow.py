"""Config flow for Valve Index Basestation integration."""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from bleak import BleakScanner, BLEDevice, BleakError
from homeassistant import config_entries
from homeassistant.components.bluetooth import async_scanner_count
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    DEFAULT_DEVICE_PREFIX,
    CONF_DISCOVERY_PREFIX,
    CONF_SETUP_METHOD,
    SETUP_AUTOMATIC,
    SETUP_SELECTION,
    SETUP_MANUAL,
    SETUP_IMPORT,
)

_LOGGER = logging.getLogger(__name__)

# MAC address regex pattern (allows formats like XX:XX:XX:XX:XX:XX or XXXXXXXXXXXX)
MAC_REGEX = r"^([0-9A-Fa-f]{2}[:-]?){5}([0-9A-Fa-f]{2})$"


class BasestationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Valve Index Basestation."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_devices: dict[str, BLEDevice] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_SETUP_METHOD, default=SETUP_AUTOMATIC): vol.In(
                            {
                                SETUP_AUTOMATIC: "Automatic Setup",
                                SETUP_SELECTION: "Select from discovered devices",
                                SETUP_MANUAL: "Manual Setup",
                            }
                        ),
                    }
                ),
            )

        # Redirect to the appropriate setup method
        setup_method = user_input[CONF_SETUP_METHOD]
        if setup_method == SETUP_AUTOMATIC:
            return await self.async_step_automatic()
        elif setup_method == SETUP_SELECTION:
            return await self.async_step_selection()
        else:
            return await self.async_step_manual()

    async def async_step_import(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Import a config entry from configuration.yaml."""
        if not user_input:
            return self.async_abort(reason="invalid_data")
        
        mac = user_input.get(CONF_MAC)
        name = user_input.get(CONF_NAME)
        
        if not mac:
            return self.async_abort(reason="invalid_mac")
        
        # Format MAC address consistently
        mac = mac.upper()
        mac = mac.replace("-", ":").replace(" ", "")
        
        # If MAC is in format without colons, add them
        if ":" not in mac and len(mac) == 12:
            mac = ":".join(mac[i:i+2] for i in range(0, 12, 2))
        
        # Use MAC address as the unique ID
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()
        
        # Create the title and name for the entry
        if name:
            title = name
        else:
            short_id = mac[-5:]
            title = f"Basestation {short_id}"
            name = title
        
        _LOGGER.info(
            "Importing basestation configuration from YAML: %s (%s)", 
            name, 
            mac
        )
        
        return self.async_create_entry(
            title=title,
            data={
                CONF_MAC: mac,
                CONF_NAME: name,
                CONF_SETUP_METHOD: SETUP_IMPORT,  # Mark as imported
            },
        )

    async def async_step_automatic(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle automatic setup."""
        errors = {}

        # Check if automatic config already exists
        for entry in self._async_current_entries():
            if entry.data.get(CONF_SETUP_METHOD) == SETUP_AUTOMATIC:
                return self.async_abort(reason="already_auto_configured")

        if user_input is None:
            return self.async_show_form(
                step_id="automatic",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_DISCOVERY_PREFIX, 
                            default=DEFAULT_DEVICE_PREFIX
                        ): str,
                    }
                ),
            )

        prefix = user_input[CONF_DISCOVERY_PREFIX]
        
        try:
            devices = await self._discover_devices()
        except Exception as ex:
            _LOGGER.error("Error discovering devices: %s", ex)
            errors["base"] = "discovery_error"
            return self.async_show_form(
                step_id="automatic",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_DISCOVERY_PREFIX, 
                            default=prefix
                        ): str,
                    }
                ),
                errors=errors,
            )

        # Filter for basestation devices
        basestation_devices = {
            addr: device
            for addr, device in devices.items()
            if device.name and device.name.startswith(prefix)
        }

        if not basestation_devices:
            return self.async_abort(reason="no_devices_found")

        # Process device names to avoid duplication
        device_entries = []
        for addr, device in basestation_devices.items():
            # Generate a simple name without duplication
            short_mac = addr.replace(":", "")[-6:]
            name = device.name  # Use device name as is, don't append anything
            
            device_entries.append({
                CONF_MAC: addr,
                CONF_NAME: name,
            })

        # Create a single config entry for automatic discovery
        return self.async_create_entry(
            title="Automatic Basestation Discovery",
            data={
                CONF_SETUP_METHOD: SETUP_AUTOMATIC,
                CONF_DISCOVERY_PREFIX: prefix,
                "devices": device_entries,
            }
        )

    async def async_step_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle device selection."""
        errors = {}
        
        try:
            devices = await self._discover_devices()
            self._discovered_devices = devices
        except Exception as ex:
            _LOGGER.error("Error discovering devices: %s", ex)
            errors["base"] = "discovery_error"
            return self.async_show_form(
                step_id="selection",
                errors=errors,
            )

        if not devices:
            return self.async_abort(reason="no_devices_found")

        if user_input is None:
            return self.async_show_form(
                step_id="selection",
                data_schema=vol.Schema(
                    {
                        vol.Required("device_selection"): vol.In(
                            {
                                addr: f"{device.name} ({addr})"
                                if device.name
                                else addr
                                for addr, device in devices.items()
                            }
                        ),
                        vol.Optional(CONF_NAME): str,
                    }
                ),
            )

        device_selection = user_input["device_selection"]
        user_provided_name = user_input.get(CONF_NAME)

        # Use device address as the unique ID
        await self.async_set_unique_id(device_selection)
        self._abort_if_unique_id_configured()

        device = devices[device_selection]
        
        # Fix for name duplication
        # Only use the user-provided name if specified, otherwise use device name
        # Don't append identifiers to either
        if user_provided_name:
            name = user_provided_name  # Use name as provided by user
            title = user_provided_name  # Use same for title
        else:
            # If device has a name, use it directly without appending anything
            if device.name:
                name = device.name
                title = device.name
            else:
                # If no device name, use a generic name with last 5 chars of MAC
                short_id = device_selection[-5:]
                name = f"Basestation {short_id}"
                title = name

        return self.async_create_entry(
            title=title,
            data={
                CONF_MAC: device_selection,
                CONF_NAME: name,
                CONF_SETUP_METHOD: SETUP_SELECTION,
            }
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual setup."""
        errors = {}

        if user_input is None:
            return self.async_show_form(
                step_id="manual",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_MAC): str,
                        vol.Optional(CONF_NAME): str,
                    }
                ),
                errors=errors,
            )

        # Format MAC address consistently
        mac = user_input[CONF_MAC].upper()
        mac = mac.replace("-", ":").replace(" ", "")
        
        # If MAC is in format without colons, add them
        if ":" not in mac and len(mac) == 12:
            mac = ":".join(mac[i:i+2] for i in range(0, 12, 2))
            
        user_provided_name = user_input.get(CONF_NAME)

        # Validate MAC address
        if not self._validate_mac(mac):
            errors["base"] = "invalid_mac"
            return self.async_show_form(
                step_id="manual",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_MAC, default=mac): str,
                        vol.Optional(CONF_NAME, default=user_provided_name): str,
                    }
                ),
                errors=errors,
            )

        # Use MAC address as the unique ID
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()
        
        # Fix for name duplication:
        # Only use the user-provided name if specified, otherwise generate a simple name
        if user_provided_name:
            name = user_provided_name
            title = user_provided_name
        else:
            short_id = mac[-5:]  # Use last 5 characters of MAC address
            name = f"Basestation {short_id}"
            title = name

        # Create entry
        return self.async_create_entry(
            title=title,
            data={
                CONF_MAC: mac,
                CONF_NAME: name,
                CONF_SETUP_METHOD: SETUP_MANUAL,
            }
        )

    def _validate_mac(self, mac: str) -> bool:
        """Validate MAC address format using regex."""
        return bool(re.match(MAC_REGEX, mac))

    async def _discover_devices(self) -> dict[str, BLEDevice]:
        """Discover BLE devices with error handling."""
        try:
            if not async_scanner_count(self.hass):
                _LOGGER.warning("No Bluetooth scanner available")
                return {}

            devices = await BleakScanner.discover()
            return {
                device.address: device
                for device in devices
                if device.address and device.name
            }
        except BleakError as err:
            _LOGGER.warning("BLE error during device discovery: %s", err)
            raise
        except Exception as ex:
            _LOGGER.error("Error during device discovery: %s", str(ex))
            raise