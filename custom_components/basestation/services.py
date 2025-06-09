"""Service handlers for basestation integration."""

import logging
from typing import cast

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_platform

from .device import ValveBasestationDevice
from .switch import BasestationSwitch

_LOGGER = logging.getLogger(__name__)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for basestation integration."""
    # Register services
    hass.services.async_register(
        "basestation",
        "identify",
        handle_identify_service,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): vol.Coerce(str),
            },
        ),
    )

    hass.services.async_register(
        "basestation",
        "set_standby",
        handle_set_standby_service,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): vol.Coerce(str),
            },
        ),
    )


async def handle_identify_service(call: ServiceCall) -> None:
    """Handle the identify service."""
    entity_id, entity = _retrieve_entity(call)
    if entity is None:
        _LOGGER.error("Entity %s not found", entity_id)
        return

    if isinstance(entity._device, ValveBasestationDevice):  # noqa: SLF001
        await entity._device.identify()  # noqa: SLF001
    else:
        _LOGGER.error("Entity %s does not belong to a ValveBasestationDevice", entity_id)


async def handle_set_standby_service(call: ServiceCall) -> None:
    """Handle the set standby service."""
    entity_id, entity = _retrieve_entity(call)
    if entity is None:
        _LOGGER.error("Entity %s not found", entity_id)
        return

    if isinstance(entity._device, ValveBasestationDevice):  # noqa: SLF001
        await entity._device.set_standby()  # noqa: SLF001
    else:
        _LOGGER.error("Entity %s does not belong to a ValveBasestationDevice", entity_id)


def _retrieve_entity(call: ServiceCall) -> tuple[str, BasestationSwitch | None]:
    """Return entity instance requested by service call."""
    entity_id = cast("str", call.data.get("entity_id"))

    # get all our platforms
    platforms = entity_platform.async_get_platforms(call.hass, "basestation")

    for platform in platforms:
        if (entity := platform.entities.get(entity_id)) is not None and isinstance(entity, BasestationSwitch):
            return entity_id, entity

    return entity_id, None
