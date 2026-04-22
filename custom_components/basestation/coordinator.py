"""DataUpdateCoordinator for VR Basestation integration."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import TYPE_CHECKING, Any, cast

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
            # Der Timeout muss größer sein als connection_timeout,
            # da die Device-Klasse eigene Retries durchführt (MAX_RETRIES = 2).
            # Gesamtdauer = (Max Versuche * connection_timeout) + Verzögerungen.
            timeout = (self.device.connection_timeout * 3) + 10
            async with asyncio.timeout(timeout):
                await self.device.update()
        except TimeoutError as err:
            msg = f"Timeout communicating with basestation {self.device.mac}"
            raise UpdateFailed(msg) from err
        except Exception as err:
            msg = f"Error communicating with basestation: {err}"
            raise UpdateFailed(msg) from err
        else:
            return {
                "is_on": self.device.is_on,
                "available": self.device.available,
                "last_power_state": self.device.last_power_state,
            }


class BasestationInfoCoordinator(DataUpdateCoordinator):
    """Class to manage fetching static device information."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: BasestationDevice,
        scan_interval: int,
    ) -> None:
        """Initialize the info coordinator."""
        self.device = device
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{device.mac}_info",
            update_interval=datetime.timedelta(seconds=scan_interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch static info from the device."""
        try:
            # Auch hier den Timeout erhöhen für mögliche Retries (INFO_READ_RETRIES = 3).
            timeout = (self.device.connection_timeout * 4) + 10
            async with asyncio.timeout(timeout):
                return cast("dict[str, Any]", await self.device.read_device_info(force=True))
        except TimeoutError as err:
            msg = f"Timeout fetching device info for {self.device.mac}"
            raise UpdateFailed(msg) from err
        except Exception as err:
            msg = f"Error fetching device info: {err}"
            raise UpdateFailed(msg) from err
