"""Device and code lifecycle management.

Bridges the persistent store with the Home Assistant device/entity
registries and keeps per-entry coordinators in sync. All UI flows and
services mutate devices/codes exclusively through this manager.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry, DeviceInfo
from homeassistant.util import slugify

from .const import (
    CONF_DUPLICATE_FROM,
    DOMAIN,
    EVENT_CODE_LEARNED,
    MANUFACTURER,
    MODEL,
)
from .coordinator import RFDeviceCoordinator
from .models import RFCode, RFDeviceConfig
from .rf_manager import RFManager
from .storage import RFDeviceStore

_LOGGER = logging.getLogger(__name__)
_DOMAIN_DATA_INIT_LOCK = f"{DOMAIN}_init_lock"


@dataclass(slots=True)
class RFDomainData:
    """Container stored in ``hass.data[DOMAIN]``."""

    store: RFDeviceStore
    rf_manager: RFManager
    device_manager: RFDeviceManager
    coordinators: dict[str, RFDeviceCoordinator] = field(default_factory=dict)


@callback
def get_domain_data(hass: HomeAssistant) -> RFDomainData:
    """Return the shared domain data (must be set up first)."""
    return hass.data[DOMAIN]


async def async_get_or_create_domain_data(hass: HomeAssistant) -> RFDomainData:
    """Return the shared domain data, initializing it on first use.

    The config flow may run before any entry has been set up, so both the
    flow and ``async_setup_entry`` bootstrap through this helper.
    """
    # Create the per-Home-Assistant lock without yielding. Concurrent callers
    # on the event loop therefore all obtain the same lock before any of them
    # can begin loading the store.
    init_lock = hass.data.get(_DOMAIN_DATA_INIT_LOCK)
    if init_lock is None:
        init_lock = hass.data[_DOMAIN_DATA_INIT_LOCK] = asyncio.Lock()

    async with init_lock:
        # Another caller may have completed initialization while we waited.
        if DOMAIN in hass.data:
            return get_domain_data(hass)

        store = RFDeviceStore(hass)
        await store.async_load()
        data = RFDomainData(
            store=store,
            rf_manager=RFManager(hass),
            device_manager=RFDeviceManager(hass, store),
        )
        hass.data[DOMAIN] = data
        return data


class RFDeviceManager:
    """CRUD operations for managed RF devices and their codes."""

    def __init__(self, hass: HomeAssistant, store: RFDeviceStore) -> None:
        """Initialize the manager."""
        self._hass = hass
        self._store = store

    # ------------------------------------------------------------------
    # Coordinators
    # ------------------------------------------------------------------

    @callback
    def async_register_coordinator(
        self, device_id: str, coordinator: RFDeviceCoordinator
    ) -> None:
        """Register the coordinator of a loaded config entry."""
        get_domain_data(self._hass).coordinators[device_id] = coordinator

    @callback
    def async_unregister_coordinator(self, device_id: str) -> None:
        """Forget the coordinator of an unloaded config entry."""
        get_domain_data(self._hass).coordinators.pop(device_id, None)

    @callback
    def _async_notify(self, device_id: str) -> None:
        """Push a store snapshot to the device's coordinator, if loaded."""
        coordinator = get_domain_data(self._hass).coordinators.get(device_id)
        if coordinator is not None:
            coordinator.async_refresh_from_store()

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    @callback
    def async_device_info(self, device: RFDeviceConfig) -> DeviceInfo:
        """Build the DeviceInfo entities attach to."""
        return DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @callback
    def async_ensure_device(
        self, entry: ConfigEntry, device: RFDeviceConfig
    ) -> DeviceEntry:
        """Create/update the device registry entry for a managed device.

        When the selected transmitter belongs to a registered device (e.g.
        an ESPHome node), it is linked as ``via_device`` so the device
        topology reflects which radio bridges the RF device.
        """
        device_registry = dr.async_get(self._hass)
        via_device: tuple[str, str] | None = None
        if device.transmitter:
            entity_registry = er.async_get(self._hass)
            transmitter_entry = entity_registry.async_get(device.transmitter)
            if transmitter_entry and transmitter_entry.device_id:
                via = device_registry.async_get(transmitter_entry.device_id)
                if via and via.identifiers:
                    via_device = next(iter(via.identifiers))

        kwargs: dict[str, Any] = {}
        if via_device is not None:
            kwargs["via_device"] = via_device
        return device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=MODEL,
            **kwargs,
        )

    @callback
    def async_update_device(
        self, entry: ConfigEntry, device: RFDeviceConfig
    ) -> None:
        """Persist device changes and sync entry title + registry name."""
        self._store.async_upsert_device(device)
        if entry.title != device.name:
            self._hass.config_entries.async_update_entry(entry, title=device.name)
        device_registry = dr.async_get(self._hass)
        registry_device = device_registry.async_get_device(
            identifiers={(DOMAIN, device.id)}
        )
        if registry_device is not None and registry_device.name != device.name:
            device_registry.async_update_device(registry_device.id, name=device.name)
        self._async_notify(device.id)

    async def async_remove_device(self, device_id: str) -> None:
        """Purge a device and all of its codes from the store."""
        rf_manager = get_domain_data(self._hass).rf_manager
        for code in self._store.async_codes_for_device(device_id).values():
            rf_manager.reset_code_rotation(code.id)
        await self._store.async_delete_device(device_id)
        self.async_unregister_coordinator(device_id)
        _LOGGER.debug("Removed RF device %s from store", device_id)

    async def async_apply_pending_duplicate(self, entry: ConfigEntry) -> None:
        """Clone codes when an entry was created via the duplicate flow.

        The config flow stores the source device id in ``entry.data``; on
        first setup the codes are cloned and the marker is removed so the
        clone happens exactly once.
        """
        source_device_id = entry.data.get(CONF_DUPLICATE_FROM)
        if not source_device_id:
            return
        device_id = entry.data["device_id"]
        count = self._store.async_duplicate_codes(source_device_id, device_id)
        _LOGGER.info(
            "Duplicated %d codes from device %s to %s",
            count,
            source_device_id,
            device_id,
        )
        new_data = {
            key: value
            for key, value in entry.data.items()
            if key != CONF_DUPLICATE_FROM
        }
        self._hass.config_entries.async_update_entry(entry, data=new_data)

    # ------------------------------------------------------------------
    # Codes
    # ------------------------------------------------------------------

    @callback
    def async_add_code(self, code: RFCode) -> None:
        """Store a new code, notify entities and fire the learned event."""
        self._store.async_upsert_code(code)
        self._async_notify(code.device_id)
        self._hass.bus.async_fire(
            EVENT_CODE_LEARNED,
            {
                "device_id": code.device_id,
                "code_id": code.id,
                "name": code.name,
                "entity_type": code.entity_type,
            },
        )
        _LOGGER.debug(
            "Added %s code '%s' (%s) to device %s",
            code.entity_type,
            code.name,
            code.id,
            code.device_id,
        )

    @callback
    def async_update_code(self, code: RFCode) -> None:
        """Persist code changes and notify entities."""
        self._store.async_upsert_code(code)
        self._async_notify(code.device_id)

    @callback
    def async_delete_code(self, code_id: str) -> None:
        """Delete a code and clean up its entity registry entry."""
        code = self._store.async_delete_code(code_id)
        if code is None:
            return
        get_domain_data(self._hass).rf_manager.reset_code_rotation(code_id)
        entity_registry = er.async_get(self._hass)
        entity_id = entity_registry.async_get_entity_id(
            code.entity_type, DOMAIN, code.unique_id
        )
        if entity_id is not None:
            entity_registry.async_remove(entity_id)
        self._async_notify(code.device_id)
        _LOGGER.debug("Deleted code %s (%s)", code.name, code_id)

    @callback
    def async_find_code(
        self, device_id: str, code_ref: str
    ) -> RFCode | None:
        """Find a code of a device by id, exact name or slugified name."""
        codes = self._store.async_codes_for_device(device_id)
        if code_ref in codes:
            return codes[code_ref]
        wanted = slugify(code_ref)
        for code in codes.values():
            if code.name == code_ref or slugify(code.name) == wanted:
                return code
        return None
