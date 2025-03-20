import voluptuous as vol
import re

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.components import bluetooth
from .const import DOMAIN

MAC_REGEX = r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$"

def validate_mac(mac):
    """Validate the MAC address format."""
    if not re.match(MAC_REGEX, mac):
        raise ValueError("Invalid MAC address format")
    return mac

class BasestationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Basestation integration."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        devices = bluetooth.async_discovered_devices(self.hass)
        device_options = {device.address: device.name for device in devices}

        if user_input is not None:
            mac = user_input["mac"].strip()
            name = user_input.get("name", "Valve Basestation").strip()
            
            try:
                validate_mac(mac)
                return self.async_create_entry(title=name, data={"mac": mac, "name": name})
            except ValueError:
                errors["mac"] = "invalid_mac"

        data_schema = vol.Schema({
            vol.Required("mac", default=next(iter(device_options), "")): vol.In(device_options) if device_options else str,
            vol.Optional("name", default="Valve Basestation"): str,
        })

        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(entry):
        return BasestationOptionsFlowHandler(entry)

class BasestationOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for the Basestation integration."""

    def __init__(self, entry):
        """Initialize options flow."""
        self.entry = entry

    async def async_step_init(self, user_input=None):
        """Manage options."""
        errors = {}
        devices = bluetooth.async_discovered_devices(self.hass)
        device_options = {device.address: device.name for device in devices}

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        data_schema = vol.Schema({
            vol.Required("mac", default=self.entry.data.get("mac")): vol.In(device_options) if device_options else str,
            vol.Optional("name", default=self.entry.data.get("name", "Valve Basestation")): str,
        })

        return self.async_show_form(step_id="init", data_schema=data_schema, errors=errors)
