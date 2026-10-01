"""DataUpdateCoordinator for VR Basestation integration."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import TYPE_CHECKING, Any, cast

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, MAX_FAST_POLLING_DURATION, BasestationPowerState

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
        fast_scan_interval: int,
    ) -> None:
        """Initialize the coordinator."""
        self.device = device

        self.default_interval = datetime.timedelta(seconds=scan_interval)
        self.fast_interval = datetime.timedelta(seconds=fast_scan_interval)
        # Monotonic time the current boot episode was first seen, None while not booting.
        self._boot_seen_at: float | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{device.mac}",
            update_interval=self.default_interval,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the device."""
        try:
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
            booting_states = (
                BasestationPowerState.STARTING_UP,
                BasestationPowerState.BOOTING_1,
                BasestationPowerState.BOOTING_2,
            )

            # If last_power_state is a booting value but hasn't been confirmed by a read recently,
            # the device has stopped responding rather than actually still booting - back off to
            # the normal interval instead of polling at fast_interval forever. "Recently" is scaled
            # to fast_interval (not a fixed constant) so this stays correct however the user has it
            # configured.
            state_age = self.device.last_power_state_age
            is_state_fresh = state_age is not None and state_age < (self.fast_interval.total_seconds() * 2)
            is_booting = self.device.last_power_state in booting_states and is_state_fresh

            # Freshness can't catch a station that keeps *answering* STARTING_UP: every fast poll
            # re-confirms it. Cap each boot episode instead, so a stuck station drops back to the
            # normal interval rather than hammering the proxy with a connect every few seconds.
            if not is_booting:
                self._boot_seen_at = None
                self.update_interval = self.default_interval
            else:
                now = time.monotonic()
                if self._boot_seen_at is None:
                    self._boot_seen_at = now
                if now - self._boot_seen_at < MAX_FAST_POLLING_DURATION:
                    self.update_interval = self.fast_interval
                else:
                    if self.update_interval != self.default_interval:
                        _LOGGER.warning(
                            "%s still reports %s after %ss, falling back to the normal poll interval",
                            self.device.mac,
                            self.device.last_power_state,
                            MAX_FAST_POLLING_DURATION,
                        )
                    self.update_interval = self.default_interval

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
            timeout = (self.device.connection_timeout * 4) + 10
            async with asyncio.timeout(timeout):
                return cast("dict[str, Any]", await self.device.read_device_info(force=True))
        except TimeoutError as err:
            if self.device.has_cached_info:
                _LOGGER.debug("Timeout fetching info, using cached data for %s", self.device.mac)
                return cast("dict[str, Any]", self.device.cached_info)

            msg = f"Timeout fetching device info for {self.device.mac}"
            raise UpdateFailed(msg) from err
        except Exception as err:
            if self.device.has_cached_info:
                _LOGGER.debug("Error fetching info, using cached data for %s", self.device.mac)
                return cast("dict[str, Any]", self.device.cached_info)

            msg = f"Error fetching device info: {err}"
            raise UpdateFailed(msg) from err
