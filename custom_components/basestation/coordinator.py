"""DataUpdateCoordinator for VR Basestation integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .device import ValveBasestationDevice

if TYPE_CHECKING:
    from .device import BasestationDevice

_LOGGER = logging.getLogger(__name__)


class BasestationCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the basestation."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: BasestationDevice,
        scan_interval: int,
    ) -> None:
        """Initialize the coordinator."""
        self.device = device
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{device.mac}",
            update_interval=timedelta(seconds=scan_interval),
        )

    async def _async_update_data(self) -> None:
        """Fetch data from the device."""
        try:
            # Dies führt das eigentliche BLE-Polling durch
            await self.device.update()
        except Exception as err:
            raise UpdateFailed(f"Error communicating with basestation: {err}") from err
