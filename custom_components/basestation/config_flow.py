"""Config flow for VR Basestation integration."""
from __future__ import annotations

import logging
import re
import asyncio
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
    DEVICE_TYPE_V1,
    DEVICE_TYPE_V2,
    V1_NAME_PREFIX,
    V2_NAME_PREFIX,
    CONF_DEVICE_TYPE,
    CONF_PAIR_ID,
    CONF_SETUP_METHOD,
    CONF_DISCOVERY_PREFIX,
    SETUP_AUTOMATIC,
    SETUP_SELECTION,
    SETUP_MANUAL,
    SETUP_IMPORT,
)

_LOGGER = logging.getLogger(__name__)

# MAC address regex pattern (allows formats like XX:XX:XX:XX:XX:XX or XXXXXXXXXXXX)
MAC_REGEX = r"^([0-9A-Fa-f]{2}[:-]?){5}([0-9A-Fa-f]{2})$"
# Pair ID regex pattern (hexadecimal value)
PAIR_ID_REGEX = r"^(0x)?[0-9A-Fa-f]{1,8}$"


class BasestationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for VR Basestation."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_devices: dict[str, BLEDevice] = {}
        self._selected_device_type: str = ""

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
                        vol.Optional(
                            CONF_DISCOVERY_PREFIX, 
                            description={"suggested_value": ""}
                        ): str,
                    }
                ),
                description_placeholders={
                    "default_prefixes": f"{V1_NAME_PREFIX} or {V2_NAME_PREFIX}"
                },
            )

        prefix = user_input.get(CONF_DISCOVERY_PREFIX, "")
        
        try:
            devices = await self._discover_devices()
        except Exception as ex:
            _LOGGER.error("Error discovering devices: %s", ex)
            errors["base"] = "discovery_error"
            return self.async_show_form(
                step_id="automatic",
                data_schema=vol.Schema(
                    {
                        vol.Optional(
                            CONF_DISCOVERY_PREFIX, 
                            default=prefix
                        ): str,
                    }
                ),
                errors=errors,
            )

        # Filter for basestation devices or use prefix if specified
        if prefix:
            basestation_devices = {
                addr: device
                for addr, device in devices.items()
                if device.name and device.name.startswith(prefix)
            }
        else:
            basestation_devices = {
                addr: device
                for addr, device in devices.items()
                if device.name and (
                    device.name.startswith(V1_NAME_PREFIX) or 
                    device.name.startswith(V2_NAME_PREFIX)
                )
            }

        if not basestation_devices:
            return self.async_abort(reason="no_devices_found")

        # Process device names to avoid duplication
        device_entries = []
        for addr, device in basestation_devices.items():
            # Determine device type
            device_type = DEVICE_TYPE_V2  # Default
            if device.name.startswith(V1_NAME_PREFIX):
                device_type = DEVICE_TYPE_V1
            
            device_entries.append({
                CONF_MAC: addr,
                CONF_NAME: device.name,
                CONF_DEVICE_TYPE: device_type,
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
            # Filter for basestation devices
            self._discovered_devices = {
                addr: device
                for addr, device in devices.items()
                if device.name and (
                    device.name.startswith(V1_NAME_PREFIX) or 
                    device.name.startswith(V2_NAME_PREFIX)
                )
            }
        except Exception as ex:
            _LOGGER.error("Error discovering devices: %s", ex)
            errors["base"] = "discovery_error"
            return self.async_show_form(
                step_id="selection",
                errors=errors,
            )

        if not self._discovered_devices:
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
                                for addr, device in self._discovered_devices.items()
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

        device = self._discovered_devices[device_selection]
        
        # Determine device type based on name
        device_type = DEVICE_TYPE_V2  # Default
        if device.name.startswith(V1_NAME_PREFIX):
            device_type = DEVICE_TYPE_V1
            # For V1 devices, we need to proceed to the pair ID step
            self._selected_device_type = device_type
            self._selected_mac = device_selection
            self._selected_name = user_provided_name or device.name
            
            return await self.async_step_pair_id()
        
        # If device has a name, use it directly without appending anything
        if user_provided_name:
            name = user_provided_name
            title = user_provided_name
        else:
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
                CONF_DEVICE_TYPE: device_type,
                CONF_SETUP_METHOD: SETUP_SELECTION,
            }
        )

    async def async_step_pair_id(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Vive basestation pair ID input."""
        errors = {}
        
        if user_input is None:
            return self.async_show_form(
                step_id="pair_id",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_PAIR_ID): str,
                    }
                ),
                description_placeholders={
                    "device_name": self._selected_name,
                },
            )
            
        pair_id_str = user_input[CONF_PAIR_ID]
        
        # Validate pair ID format
        if not re.match(PAIR_ID_REGEX, pair_id_str):
            errors[CONF_PAIR_ID] = "invalid_pair_id"
            return self.async_show_form(
                step_id="pair_id",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_PAIR_ID): str,
                    }
                ),
                errors=errors,
                description_placeholders={
                    "device_name": self._selected_name,
                },
            )
            
        # Convert pair ID to integer
        if pair_id_str.startswith("0x"):
            pair_id = int(pair_id_str, 16)
        else:
            try:
                pair_id = int(pair_id_str, 16)
            except ValueError:
                pair_id = int(pair_id_str)
        
        return self.async_create_entry(
            title=self._selected_name,
            data={
                CONF_MAC: self._selected_mac,
                CONF_NAME: self._selected_name,
                CONF_DEVICE_TYPE: self._selected_device_type,
                CONF_PAIR_ID: pair_id,
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
                        vol.Required(CONF_DEVICE_TYPE, default=DEVICE_TYPE_V2): vol.In(
                            {
                                DEVICE_TYPE_V2: "Valve Basestation (V2)",
                                DEVICE_TYPE_V1: "Vive Basestation (V1)",
                            }
                        ),
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
        device_type = user_input[CONF_DEVICE_TYPE]

        # Validate MAC address
        if not self._validate_mac(mac):
            errors["base"] = "invalid_mac"
            return self.async_show_form(
                step_id="manual",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_MAC, default=mac): str,
                        vol.Optional(CONF_NAME, default=user_provided_name): str,
                        vol.Required(CONF_DEVICE_TYPE, default=device_type): vol.In(
                            {
                                DEVICE_TYPE_V2: "Valve Basestation (V2)",
                                DEVICE_TYPE_V1: "Vive Basestation (V1)",
                            }
                        ),
                    }
                ),
                errors=errors,
            )

        # Use MAC address as the unique ID
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()
        
        # If this is a V1 device, we need the pair ID
        if device_type == DEVICE_TYPE_V1:
            self._selected_device_type = device_type
            self._selected_mac = mac
            self._selected_name = user_provided_name or f"Vive Basestation {mac[-5:]}"
            
            return await self.async_step_pair_id()
        
        # For V2 devices, finish the process
        if user_provided_name:
            name = user_provided_name
            title = user_provided_name
        else:
            short_id = mac[-5:]
            name = f"Valve Basestation {short_id}"
            title = name

        return self.async_create_entry(
            title=title,
            data={
                CONF_MAC: mac,
                CONF_NAME: name,
                CONF_DEVICE_TYPE: device_type,
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