"""Service handlers for basestation integration."""
import logging
import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.helpers import entity_registry
from homeassistant.helpers.entity_platform import async_get_platforms

from .device import ValveBasestationDevice

_LOGGER = logging.getLogger(__name__)

async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for basestation integration."""
    er = entity_registry.async_get(hass)
    component = EntityComponent(_LOGGER, SWITCH_DOMAIN, hass)
    
    async def handle_identify_service(call: ServiceCall) -> None:
        """Handle the identify service."""
        entity_id = call.data.get("entity_id")
        entity_entry = er.async_get(entity_id)
        
        if not entity_entry:
            _LOGGER.error("Entity %s not found", entity_id)
            return
        
        device_id = entity_entry.device_id
        
        # Find all platforms for the basestation integration
        platforms = async_get_platforms(hass, "basestation")
        
        # Find entities on this device
        for platform in platforms:
            for entity in platform.entities.values():
                if hasattr(entity, "device_info") and entity.device_info.get("identifiers") and entity.device_info.get("identifiers")[0][1] == device_id:
                    # Get the device instance and call identify
                    if hasattr(entity, "_device") and isinstance(entity._device, ValveBasestationDevice):
                        await entity._device.identify()
                        return
        
        _LOGGER.error("Could not find ValveBasestationDevice for entity %s", entity_id)
    
    async def handle_set_standby_service(call: ServiceCall) -> None:
        """Handle the set standby service."""
        entity_id = call.data.get("entity_id")
        entity_entry = er.async_get(entity_id)
        
        if not entity_entry:
            _LOGGER.error("Entity %s not found", entity_id)
            return
        
        device_id = entity_entry.device_id
        
        # Find all platforms for the basestation integration
        platforms = async_get_platforms(hass, "basestation")
        
        # Find entities on this device
        for platform in platforms:
            for entity in platform.entities.values():
                if hasattr(entity, "device_info") and entity.device_info.get("identifiers") and entity.device_info.get("identifiers")[0][1] == device_id:
                    # Get the device instance and call set_standby
                    if hasattr(entity, "_device") and isinstance(entity._device, ValveBasestationDevice):
                        await entity._device.set_standby()
                        return
        
        _LOGGER.error("Could not find ValveBasestationDevice for entity %s", entity_id)
    
    # Register services
    hass.services.async_register(
        "basestation", "identify", handle_identify_service,
        schema=vol.Schema({
            vol.Required("entity_id"): vol.Coerce(str),
        })
    )
    
    hass.services.async_register(
        "basestation", "set_standby", handle_set_standby_service,
        schema=vol.Schema({
            vol.Required("entity_id"): vol.Coerce(str),
        })
    )