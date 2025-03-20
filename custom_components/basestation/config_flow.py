"""Config flow for Valve Index Basestation integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from bleak import BleakScanner, BLEDevice
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
)

_LOGGER = logging.getLogger(__name__)


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

        if user_input[CONF_SETUP_METHOD] == SETUP_AUTOMATIC:
            return await self.async_step_automatic()
        elif user_input[CONF_SETUP_METHOD] == SETUP_SELECTION:
            return await self.async_step_selection()
        else:
            return await self.async_step_manual()

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
        devices = await self._discover_devices()

        # Filter for basestation devices
        basestation_devices = {
            addr: device
            for addr, device in devices.items()
            if device.name and device.name.startswith(prefix)
        }

        if not basestation_devices:
            return self.async_abort(reason="no_devices_found")

        # Create a single config entry for automatic discovery
        return self.async_create_entry(
            title="Automatic Basestation Discovery",
            data={
                CONF_SETUP_METHOD: SETUP_AUTOMATIC,
                CONF_DISCOVERY_PREFIX: prefix,
                "devices": [
                    {
                        CONF_MAC: addr,
                        CONF_NAME: device.name,
                    }
                    for addr, device in basestation_devices.items()
                ],
            }
        )

    async def async_step_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle device selection."""
        errors = {}
        devices = await self._discover_devices()
        self._discovered_devices = devices

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
        name = user_input.get(CONF_NAME)

        await self.async_set_unique_id(device_selection)
        self._abort_if_unique_id_configured()

        device = devices[device_selection]
        title = name or (f"Basestation {device.name}" if device.name else f"Basestation {device_selection}")

        return self.async_create_entry(
            title=title,
            data={
                CONF_MAC: device_selection,
                CONF_NAME: name or device.name or title,
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

        mac = user_input[CONF_MAC].upper()
        name = user_input.get(CONF_NAME)

        if not self._validate_mac(mac):
            errors["base"] = "invalid_mac"
            return self.async_show_form(
                step_id="manual",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_MAC, default=mac): str,
                        vol.Optional(CONF_NAME, default=name): str,
                    }
                ),
                errors=errors,
            )

        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=name or f"Basestation {mac}",
            data={
                CONF_MAC: mac,
                CONF_NAME: name or f"Basestation {mac}",
                CONF_SETUP_METHOD: SETUP_MANUAL,
            }
        )

    def _validate_mac(self, mac: str) -> bool:
        """Validate MAC address format."""
        try:
            mac = mac.replace(":", "").replace("-", "")
            int(mac, 16)
            return len(mac) == 12
        except ValueError:
            return False

    async def _discover_devices(self) -> dict[str, BLEDevice]:
        """Discover BLE devices."""
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

        except Exception as ex:
            _LOGGER.exception("Error during device discovery: %s", str(ex))
            return {}
