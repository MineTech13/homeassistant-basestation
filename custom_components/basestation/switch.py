"""The basestation switch component."""

import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_TYPE,
    CONF_PAIR_ID,
    DOMAIN,
    STANDBY_STATE_VALUE,
    STANDBY_SWITCH_SCAN_INTERVAL,
)
from .device import (
    BasestationDevice,
    ValveBasestationDevice,
    get_basestation_device,
)

_LOGGER = logging.getLogger(__name__)

# Track MAC addresses that have been notified about migration
NOTIFIED_MACS = set()

# Constants for magic numbers
MAC_LENGTH_NO_SEPARATORS = 12
MAC_SEPARATOR_INTERVAL = 2


def setup_platform(
    hass: HomeAssistant,
    config: dict,
    _add_entities: Any,
    _discovery_info: Any = None,
) -> bool:
    """Set up the basestation platform from YAML configuration and trigger migration."""
    _LOGGER.info("Found YAML configuration for basestation, starting migration to config entries")

    mac = config.get(CONF_MAC)
    name = config.get(CONF_NAME)

    if not mac:
        _LOGGER.error("MAC address is required for basestation setup")
        return False

    # Format MAC address consistently
    formatted_mac = mac.replace(":", "").replace("-", "").replace(" ", "").upper()
    if len(formatted_mac) == MAC_LENGTH_NO_SEPARATORS:
        formatted_mac = ":".join(
            formatted_mac[i : i + MAC_SEPARATOR_INTERVAL]
            for i in range(0, MAC_LENGTH_NO_SEPARATORS, MAC_SEPARATOR_INTERVAL)
        )

    # Check if we've already processed this MAC address
    if formatted_mac in NOTIFIED_MACS:
        _LOGGER.debug("Already processed migration for %s", formatted_mac)
        return True

    NOTIFIED_MACS.add(formatted_mac)

    # Prepare import data
    import_data = {
        CONF_MAC: formatted_mac,
        CONF_NAME: name,
    }

    # Define async function to handle the import flow
    async def async_start_import() -> None:
        """Start the import flow asynchronously."""
        try:
            _LOGGER.info("Starting import flow for basestation %s (%s)", name or "Unnamed", formatted_mac)
            await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}, data=import_data)
            _LOGGER.info("Import flow started successfully for %s", formatted_mac)
        except Exception:
            _LOGGER.exception("Failed to start import flow for %s", formatted_mac)

    # Use hass.add_job() to schedule the async work - this is thread-safe
    hass.add_job(async_start_import())

    # Create a notification about the migration
    notification_data = {
        "message": (
            f"Found Valve Basestation '{name if name else formatted_mac}' configured in YAML.\n\n"
            f"The integration is automatically migrating this device to the UI configuration. "
            f"Once the migration is complete, you can safely remove this entry from your configuration.yaml:\n\n"
            f"```yaml\nswitch:\n  - platform: basestation\n    "
            f"mac: '{mac}'\n    name: '{name if name else 'Valve Basestation'}'\n```\n\n"
            f"The migrated device will appear in Settings → Devices & Services."
        ),
        "title": "Valve Basestation: Configuration Migration",
        "notification_id": f"basestation_migration_{formatted_mac.replace(':', '_')}",
    }

    # Create the notification using thread-safe service call
    hass.services.call("persistent_notification", "create", notification_data)

    # Don't set up any entities from YAML - they will be created by the config entry
    _LOGGER.debug("YAML platform setup completed for %s, entities will be created by config entry", formatted_mac)
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the basestation switch from a config entry."""
    _LOGGER.debug("Setting up basestation switch entities for entry: %s", entry.title)

    # Get device configuration from the config entry
    mac = entry.data.get(CONF_MAC)
    name = entry.data.get(CONF_NAME)
    device_type = entry.data.get(CONF_DEVICE_TYPE)
    pair_id = entry.data.get(CONF_PAIR_ID)
    setup_method = entry.data.get("setup_method", "unknown")

    if not mac:
        _LOGGER.error("No MAC address found in config entry data: %s", entry.data)
        return

    _LOGGER.debug("Creating device for MAC: %s, Name: %s, Type: %s, Method: %s", mac, name, device_type, setup_method)

    # Create the basestation device instance
    device = get_basestation_device(hass, mac, name=name, device_type=device_type, pair_id=pair_id)

    if not device:
        _LOGGER.error("Failed to create device for MAC: %s", mac)
        return

    # Create the switch entities for this device
    entities = [BasestationSwitch(device, entry.entry_id)]

    # Add standby switch for Valve basestations (V2 devices)
    if isinstance(device, ValveBasestationDevice):
        entities.append(BasestationStandbySwitch(device, entry.entry_id))
        _LOGGER.debug("Added standby switch for Valve basestation: %s", mac)

    # Add all entities to Home Assistant
    async_add_entities(entities, update_before_add=True)
    _LOGGER.info("Successfully added %d switch entities for %s setup: %s", len(entities), setup_method, name or mac)


class BasestationSwitch(SwitchEntity):
    """Representation of a basestation main power switch."""

    def __init__(self, device: BasestationDevice, entry_id: str) -> None:
        """Initialize the switch."""
        self._device = device
        self._entry_id = entry_id
        self._attr_unique_id = f"basestation_{device.mac}"
        self._attr_name = device.device_name
        self._attr_icon = "mdi:virtual-reality"
        self._last_update = 0

        # Create device info for Home Assistant device registry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
            name=device.device_name,
            manufacturer=("Valve" if isinstance(device, ValveBasestationDevice) else "HTC"),
            model=("Index Basestation" if isinstance(device, ValveBasestationDevice) else "Vive Basestation"),
        )

        _LOGGER.debug("Initialized BasestationSwitch for %s (%s)", device.device_name, device.mac)

    @property
    def is_on(self) -> bool:
        """Return if the switch is currently on or off."""
        return self._device.is_on

    @property
    def available(self) -> bool:
        """Return if the device is available."""
        return self._device.available

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn the switch on."""
        _LOGGER.debug("Turning on basestation: %s", self._device.mac)
        await self._device.turn_on()
        self.async_write_ha_state()

        # Force update of standby switch if it exists
        standby_entity_id = f"switch.{self._device.device_name.lower().replace(' ', '_')}_standby_mode"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": standby_entity_id},
                blocking=False,
            ),
        )

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn the switch off."""
        _LOGGER.debug("Turning off basestation: %s", self._device.mac)
        await self._device.turn_off()
        self.async_write_ha_state()

        # Force update of standby switch if it exists
        standby_entity_id = f"switch.{self._device.device_name.lower().replace(' ', '_')}_standby_mode"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": standby_entity_id},
                blocking=False,
            ),
        )

    async def async_update(self) -> None:
        """Fetch new state data for the sensor."""
        await self._device.update()


class BasestationStandbySwitch(SwitchEntity):
    """Representation of a basestation standby switch (V2 only)."""

    def __init__(self, device: BasestationDevice, entry_id: str) -> None:
        """Initialize the standby switch."""
        self._device = device
        self._entry_id = entry_id
        self._attr_unique_id = f"basestation_{device.mac}_standby"
        self._attr_name = f"{device.device_name} Standby Mode"
        self._attr_icon = "mdi:sleep"
        self._is_in_standby = False
        self._last_update = 0.0

        # Share device info with main switch
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
        )

        _LOGGER.debug("Initialized BasestationStandbySwitch for %s", device.mac)

    @property
    def is_on(self) -> bool:
        """Return if standby mode is active."""
        return self._is_in_standby

    @property
    def available(self) -> bool:
        """Return if the device is available."""
        return self._device.available

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn on standby mode (instead of full sleep)."""
        if isinstance(self._device, ValveBasestationDevice):
            _LOGGER.debug("Setting standby mode on for: %s", self._device.mac)
            await self._device.set_standby()
            self._is_in_standby = True
            self.async_write_ha_state()

            # Force refresh of power switch
            power_entity_id = f"switch.{self._device.device_name.lower().replace(' ', '_')}"
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "homeassistant",
                    "update_entity",
                    {"entity_id": power_entity_id},
                    blocking=False,
                ),
            )

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn off standby mode (device will go to full on mode)."""
        if isinstance(self._device, ValveBasestationDevice):
            _LOGGER.debug("Setting standby mode off for: %s", self._device.mac)
            await self._device.turn_on()
            self._is_in_standby = False
            self.async_write_ha_state()

            # Force refresh of power switch
            power_entity_id = f"switch.{self._device.device_name.lower().replace(' ', '_')}"
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "homeassistant",
                    "update_entity",
                    {"entity_id": power_entity_id},
                    blocking=False,
                ),
            )

    async def async_update(self) -> None:
        """Update the standby state based on device state."""
        current_time = time.time()
        if current_time - self._last_update < STANDBY_SWITCH_SCAN_INTERVAL:
            return

        if isinstance(self._device, ValveBasestationDevice):
            # Get the raw power state value to determine if in standby mode
            raw_state = await self._device.get_raw_power_state()

            # Update standby state - 0x02 is the standby state value
            if raw_state == STANDBY_STATE_VALUE:
                if not self._is_in_standby:
                    self._is_in_standby = True
                    _LOGGER.debug("Standby state changed to ON for %s", self._device.mac)
            elif raw_state is not None and self._is_in_standby:  # Only update if we have a valid state
                self._is_in_standby = False
                _LOGGER.debug("Standby state changed to OFF for %s", self._device.mac)

            self._last_update = current_time
