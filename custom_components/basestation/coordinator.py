"""DataUpdateCoordinator for VR Basestation integration."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

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
            update_interval=datetime.timedelta(seconds=scan_interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the device."""
        try:
            # Timeout von 10 Sekunden für das Update festlegen
            async with asyncio.timeout(10.0):
                await self.device.update()
        except TimeoutError as err:
            raise UpdateFailed(f"Timeout communicating with basestation {self.device.mac}") from err
        except Exception as err:
            raise UpdateFailed(f"Error communicating with basestation: {err}") from err
        else:
            return {
                "is_on": self.device.is_on,
                "available": self.device.available,
                "last_power_state": self.device.last_power_state,
            }
