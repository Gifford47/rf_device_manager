"""RF Device Manager - manage RF devices entirely from the Home Assistant UI.

Creates one config entry per managed RF device. Sending uses the
``radio_frequency`` entity platform introduced in Home Assistant 2026.5
(ESPHome ``ir_rf_proxy`` transmitters and other compatible hardware);
learning/receiving uses the ESPHome native API receive stream plus an
event-bus fallback. All devices, codes and entities are created, edited and
deleted through config/options flows - no YAML required.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    issue_registry as ir,
)
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEFAULT_REPEAT,
    CONF_DESCRIPTION,
    CONF_DEVICE_ID,
    CONF_ENABLED,
    CONF_FREQUENCY,
    CONF_NAME,
    CONF_TRANSMITTER,
    DEFAULT_FREQUENCY,
    DEFAULT_REPEAT,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import RFDeviceCoordinator, RFDeviceManagerConfigEntry
from .device_manager import async_get_or_create_domain_data, get_domain_data
from .models import RFDeviceConfig
from .repairs import async_check_transmitter_issue, issue_id_for_entry
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (storage + domain services)."""
    await async_get_or_create_domain_data(hass)
    async_setup_services(hass)
    return True


def _sync_store_from_entry(
    hass: HomeAssistant, entry: RFDeviceManagerConfigEntry
) -> RFDeviceConfig:
    """Make the store record reflect the entry data/options (entry wins)."""
    store = get_domain_data(hass).store
    device_id = entry.data[CONF_DEVICE_ID]
    device = store.async_get_device(device_id) or RFDeviceConfig(
        id=device_id, name=entry.title
    )
    device.name = entry.options.get(CONF_NAME) or entry.title
    device.description = entry.options.get(CONF_DESCRIPTION, "")
    device.transmitter = entry.options.get(CONF_TRANSMITTER, "")
    device.frequency = int(entry.options.get(CONF_FREQUENCY, DEFAULT_FREQUENCY))
    device.default_repeat = int(
        entry.options.get(CONF_DEFAULT_REPEAT, DEFAULT_REPEAT)
    )
    device.enabled = bool(entry.options.get(CONF_ENABLED, True))
    store.async_upsert_device(device)
    return device


async def async_setup_entry(
    hass: HomeAssistant, entry: RFDeviceManagerConfigEntry
) -> bool:
    """Set up one managed RF device."""
    domain_data = await async_get_or_create_domain_data(hass)
    device = _sync_store_from_entry(hass, entry)
    await domain_data.device_manager.async_apply_pending_duplicate(entry)

    coordinator = RFDeviceCoordinator(hass, entry, domain_data.store, device.id)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    domain_data.device_manager.async_register_coordinator(device.id, coordinator)
    domain_data.device_manager.async_ensure_device(entry, device)

    async_check_transmitter_issue(hass, entry)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: RFDeviceManagerConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: RFDeviceManagerConfigEntry
) -> bool:
    """Unload a managed RF device."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and DOMAIN in hass.data:
        get_domain_data(hass).device_manager.async_unregister_coordinator(
            entry.data[CONF_DEVICE_ID]
        )
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow deleting the RF device from the device registry UI.

    Each config entry manages exactly one device, so removing the device
    removes the whole entry - including all stored codes and entities.
    The entry removal is scheduled as a task because Home Assistant is
    still processing the device removal when this hook runs.
    """
    hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: RFDeviceManagerConfigEntry
) -> None:
    """Purge the device and its codes when the entry is deleted."""
    domain_data = await async_get_or_create_domain_data(hass)
    await domain_data.device_manager.async_remove_device(entry.data[CONF_DEVICE_ID])
    ir.async_delete_issue(hass, DOMAIN, issue_id_for_entry(entry.entry_id))


async def async_migrate_entry(
    hass: HomeAssistant, entry: RFDeviceManagerConfigEntry
) -> bool:
    """Migrate old config entries to the current version."""
    if entry.version > 1:
        # Downgrade from a future version is not supported.
        return False
    if entry.version == 1 and entry.minor_version < 2:
        # Before 1.2, a default repeat of 0 was sent as DEFAULT_REPEAT (7)
        # extra repeats; since 1.2, 0 sends exactly once. Keep existing
        # devices transmitting as before.
        options = dict(entry.options)
        if int(options.get(CONF_DEFAULT_REPEAT, 0) or 0) <= 0:
            options[CONF_DEFAULT_REPEAT] = DEFAULT_REPEAT
        hass.config_entries.async_update_entry(
            entry, options=options, minor_version=2
        )
        _LOGGER.debug("Migrated config entry %s to 1.2", entry.entry_id)
    return True
