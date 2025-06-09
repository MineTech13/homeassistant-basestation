"""Config flow for VR Basestation integration."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import callback

from .const import (
    CONF_CONNECTION_TIMEOUT,
    CONF_DEVICE_TYPE,
    CONF_ENABLE_INFO_SENSORS,
    CONF_ENABLE_POWER_STATE_SENSOR,
    CONF_INFO_SCAN_INTERVAL,
    CONF_PAIR_ID,
    CONF_POWER_STATE_SCAN_INTERVAL,
    CONF_SETUP_METHOD,
    CONF_STANDBY_SCAN_INTERVAL,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_INFO_SCAN_INTERVAL,
    DEFAULT_POWER_STATE_SCAN_INTERVAL,
    DEFAULT_STANDBY_SCAN_INTERVAL,
    DEVICE_TYPE_V1,
    DEVICE_TYPE_V2,
    DOMAIN,
    SETUP_IMPORT,
    SETUP_MANUAL,
    V1_NAME_PREFIX,
    V2_NAME_PREFIX,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigFlowResult, OptionsFlow

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
        self._selected_device_type: str = ""
        # Store discovery info for bluetooth discovery flows
        self._discovery_info: BluetoothServiceInfoBleak | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return BasestationOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step - go directly to manual setup."""
        # When user manually adds integration, go straight to manual setup
        return await self.async_step_manual()

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """Handle bluetooth discovery from Home Assistant's bluetooth integration."""
        _LOGGER.debug("Bluetooth discovery triggered for device: %s (%s)", discovery_info.name, discovery_info.address)

        # Store the discovery info
        self._discovery_info = discovery_info

        # Extract device information from the BluetoothServiceInfoBleak object
        mac = discovery_info.address
        name = discovery_info.name or "Unknown Basestation"

        # Determine device type based on name
        device_type = DEVICE_TYPE_V2  # Default to V2
        if name.startswith(V1_NAME_PREFIX):
            device_type = DEVICE_TYPE_V1
        elif name.startswith(V2_NAME_PREFIX):
            device_type = DEVICE_TYPE_V2

        _LOGGER.info("Discovered %s basestation: %s (%s)", "V1" if device_type == DEVICE_TYPE_V1 else "V2", name, mac)

        # Use MAC address as the unique ID
        await self.async_set_unique_id(mac.upper())
        self._abort_if_unique_id_configured()

        # Set title for the discovery flow
        self.context["title_placeholders"] = {
            "name": name,
            "mac": mac[-5:],  # Show last 5 chars of MAC
            "device_type": "Valve Basestation (V2)" if device_type == DEVICE_TYPE_V2 else "Vive Basestation (V1)",
        }

        # Store device type for later use
        self._selected_device_type = device_type

        # Show confirmation step for discovered device
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm setup of discovered basestation."""
        if not self._discovery_info:
            _LOGGER.error("No discovery info available for bluetooth confirmation")
            return self.async_abort(reason="invalid_data")

        mac = self._discovery_info.address
        name = self._discovery_info.name or "Unknown Basestation"
        device_type = self._selected_device_type

        if user_input is None:
            return self.async_show_form(
                step_id="bluetooth_confirm",
                data_schema=vol.Schema(
                    {
                        vol.Optional(CONF_NAME, default=name): str,
                    }
                ),
                description_placeholders={
                    "name": name,
                    "mac": mac,
                    "device_type": "Valve Basestation (V2)"
                    if device_type == DEVICE_TYPE_V2
                    else "Vive Basestation (V1)",
                },
            )

        user_provided_name = user_input.get(CONF_NAME, name)

        # If this is a V1 device, we need the pair ID
        if device_type == DEVICE_TYPE_V1:
            self._selected_mac = mac
            self._selected_name = user_provided_name
            return await self.async_step_pair_id()

        # For V2 devices, create the entry directly
        title = user_provided_name if user_provided_name else f"Basestation {mac[-5:]}"

        return self.async_create_entry(
            title=title,
            data={
                CONF_MAC: mac.upper(),
                CONF_NAME: user_provided_name,
                CONF_DEVICE_TYPE: device_type,
                CONF_SETUP_METHOD: "bluetooth_discovery",
            },
        )

    async def async_step_import(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
        if ":" not in mac and len(mac) == 12:  # noqa: PLR2004
            mac = ":".join(mac[i : i + 2] for i in range(0, 12, 2))

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

        _LOGGER.info("Importing basestation configuration from YAML: %s (%s)", name, mac)

        return self.async_create_entry(
            title=title,
            data={
                CONF_MAC: mac,
                CONF_NAME: name,
                CONF_SETUP_METHOD: SETUP_IMPORT,  # Mark as imported
            },
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle manual setup - this is the main manual entry point."""
        errors: dict[str, str] = {}

        if user_input is None:
            # Show the manual setup form
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
                            },
                        ),
                    },
                ),
                errors=errors,
            )

        # Process the submitted form data
        mac = user_input[CONF_MAC].upper()
        mac = mac.replace("-", ":").replace(" ", "")

        # If MAC is in format without colons, add them
        if ":" not in mac and len(mac) == 12:  # noqa: PLR2004
            mac = ":".join(mac[i : i + 2] for i in range(0, 12, 2))

        user_provided_name = user_input.get(CONF_NAME)
        device_type = user_input[CONF_DEVICE_TYPE]

        # Validate MAC address format
        if not _validate_mac(mac):
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
                            },
                        ),
                    },
                ),
                errors=errors,
            )

        # Use MAC address as the unique ID to prevent duplicates
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()

        # If this is a V1 device, we need the pair ID before we can continue
        if device_type == DEVICE_TYPE_V1:
            self._selected_device_type = device_type
            self._selected_mac = mac
            self._selected_name = user_provided_name or f"Vive Basestation {mac[-5:]}"
            return await self.async_step_pair_id()

        # For V2 devices, we can create the entry immediately
        if user_provided_name:
            name = user_provided_name
            title = user_provided_name
        else:
            # Generate a friendly name using the last 5 characters of MAC
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
            },
        )

    async def async_step_pair_id(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle Vive basestation pair ID input for V1 devices."""
        errors = {}

        if user_input is None:
            # Show the pair ID input form
            return self.async_show_form(
                step_id="pair_id",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_PAIR_ID): str,
                    },
                ),
                description_placeholders={
                    "device_name": self._selected_name,
                },
            )

        pair_id_str = user_input[CONF_PAIR_ID]

        # Validate pair ID format (should be hexadecimal)
        if not re.match(PAIR_ID_REGEX, pair_id_str):
            errors[CONF_PAIR_ID] = "invalid_pair_id"
            return self.async_show_form(
                step_id="pair_id",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_PAIR_ID): str,
                    },
                ),
                errors=errors,
                description_placeholders={
                    "device_name": self._selected_name,
                },
            )

        # Convert pair ID string to integer
        try:
            if pair_id_str.startswith("0x"):
                pair_id = int(pair_id_str, 16)
            else:
                # Try hex first, then decimal if that fails
                try:
                    pair_id = int(pair_id_str, 16)
                except ValueError:
                    pair_id = int(pair_id_str)
        except ValueError:
            errors[CONF_PAIR_ID] = "invalid_pair_id"
            return self.async_show_form(
                step_id="pair_id",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_PAIR_ID): str,
                    },
                ),
                errors=errors,
                description_placeholders={
                    "device_name": self._selected_name,
                },
            )

        # Create the config entry for the V1 basestation
        return self.async_create_entry(
            title=self._selected_name,
            data={
                CONF_MAC: self._selected_mac,
                CONF_NAME: self._selected_name,
                CONF_DEVICE_TYPE: self._selected_device_type,
                CONF_PAIR_ID: pair_id,
                CONF_SETUP_METHOD: SETUP_MANUAL,
            },
        )


class BasestationOptionsFlow(config_entries.OptionsFlow):
    """Handle Basestation options flow."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize the options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        return await self.async_step_device_options()

    async def async_step_device_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle device-specific options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate scan intervals
            if user_input.get(CONF_INFO_SCAN_INTERVAL, 0) < 300:  # Minimum 5 minutes
                errors[CONF_INFO_SCAN_INTERVAL] = "scan_interval_too_low"

            if user_input.get(CONF_POWER_STATE_SCAN_INTERVAL, 0) < 1:  # Minimum 1 second
                errors[CONF_POWER_STATE_SCAN_INTERVAL] = "scan_interval_too_low"

            if user_input.get(CONF_STANDBY_SCAN_INTERVAL, 0) < 1:  # Minimum 1 second
                errors[CONF_STANDBY_SCAN_INTERVAL] = "scan_interval_too_low"

            if user_input.get(CONF_CONNECTION_TIMEOUT, 0) < 5:  # Minimum 5 seconds
                errors[CONF_CONNECTION_TIMEOUT] = "timeout_too_low"

            if not errors:
                # Update the config entry with new options
                return self.async_create_entry(title="", data=user_input)

        # Get current options or set defaults
        current_options = self.config_entry.options
        device_type = self.config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_V2)
        current_name = current_options.get(CONF_NAME, self.config_entry.data.get(CONF_NAME, ""))

        # Build the schema based on device type
        schema_dict = {
            vol.Optional(CONF_NAME, default=current_name, description="Custom device name"): str,
            vol.Optional(
                CONF_INFO_SCAN_INTERVAL,
                default=current_options.get(CONF_INFO_SCAN_INTERVAL, DEFAULT_INFO_SCAN_INTERVAL),
                description="How often to scan for device info (seconds, minimum 300)",
            ): vol.All(vol.Coerce(int), vol.Range(min=300, max=86400)),
            vol.Optional(
                CONF_CONNECTION_TIMEOUT,
                default=current_options.get(CONF_CONNECTION_TIMEOUT, DEFAULT_CONNECTION_TIMEOUT),
                description="BLE connection timeout (seconds, minimum 5)",
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=60)),
            vol.Optional(
                CONF_ENABLE_INFO_SENSORS,
                default=current_options.get(CONF_ENABLE_INFO_SENSORS, True),
                description="Enable device information sensors (firmware, model, etc.)",
            ): bool,
        }

        # Add V2-specific options
        if device_type == DEVICE_TYPE_V2:
            schema_dict.update(
                {
                    vol.Optional(
                        CONF_POWER_STATE_SCAN_INTERVAL,
                        default=current_options.get(CONF_POWER_STATE_SCAN_INTERVAL, DEFAULT_POWER_STATE_SCAN_INTERVAL),
                        description="How often to check power state (seconds, minimum 1)",
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
                    vol.Optional(
                        CONF_STANDBY_SCAN_INTERVAL,
                        default=current_options.get(CONF_STANDBY_SCAN_INTERVAL, DEFAULT_STANDBY_SCAN_INTERVAL),
                        description="How often to check standby state (seconds, minimum 1)",
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
                    vol.Optional(
                        CONF_ENABLE_POWER_STATE_SENSOR,
                        default=current_options.get(CONF_ENABLE_POWER_STATE_SENSOR, True),
                        description="Enable power state sensor",
                    ): bool,
                }
            )

        return self.async_show_form(
            step_id="device_options",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "device_name": self.config_entry.title,
                "device_type": "Valve Basestation (V2)" if device_type == DEVICE_TYPE_V2 else "Vive Basestation (V1)",
            },
        )


def _validate_mac(mac: str) -> bool:
    """Validate MAC address format using regex."""
    return bool(re.match(MAC_REGEX, mac))
