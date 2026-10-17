"""Persistent storage for the RF Device Manager integration.

Devices and RF codes are persisted with the Home Assistant Storage API in
``.storage/rf_device_manager`` (no YAML). The store keeps large raw timing
arrays out of the config entry registry; config entries only carry device
identity and mutable settings.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_REPEAT,
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
)
from .models import RFCode, RFDeviceConfig

_LOGGER = logging.getLogger(__name__)


class _RFStore(Store[dict[str, Any]]):
    """Store subclass with schema migration support."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Migrate stored data to the current schema version.

        Version 1.1 is the initial schema; each later version transforms the
        data here step by step.
        """
        data = old_data
        _LOGGER.debug(
            "Migrating %s storage from %s.%s",
            STORAGE_KEY,
            old_major_version,
            old_minor_version,
        )
        if old_major_version == 1 and old_minor_version < 2:
            data = _migrate_repeat_zero(data)
        return data


def _legacy_repeat(value: Any) -> Any:
    """Map a pre-1.2 repeat value to its current meaning.

    Before 1.2, a repeat of 0 was silently sent as DEFAULT_REPEAT (7) extra
    repeats. Since 1.2, 0 sends exactly once, so stored zeros become 7 to
    keep every existing device and code transmitting exactly as before.
    """
    try:
        return DEFAULT_REPEAT if int(value) <= 0 else value
    except (TypeError, ValueError):
        return value


def _migrate_repeat_zero(data: dict[str, Any]) -> dict[str, Any]:
    """Storage 1.1 -> 1.2: rewrite legacy repeat=0 values (see above)."""
    for device in (data.get("devices") or {}).values():
        settings = device.get("settings") or {}
        if "default_repeat" in settings:
            settings["default_repeat"] = _legacy_repeat(settings["default_repeat"])
    for code in (data.get("codes") or {}).values():
        settings = code.get("settings") or {}
        if "repeat" in settings:
            settings["repeat"] = _legacy_repeat(settings["repeat"])
        commands = (code.get("rf_data") or {}).get("commands") or {}
        for command in commands.values():
            if "repeat" in command:
                command["repeat"] = _legacy_repeat(command["repeat"])
    return data


class RFDeviceStore:
    """In-memory model of all managed devices and codes, backed by a Store."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the store wrapper."""
        self._hass = hass
        self._store = _RFStore(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY,
            minor_version=STORAGE_MINOR_VERSION,
        )
        self.devices: dict[str, RFDeviceConfig] = {}
        self.codes: dict[str, RFCode] = {}
        self._loaded = False

    async def async_load(self) -> None:
        """Load data from disk once."""
        if self._loaded:
            return
        self._loaded = True
        data = await self._store.async_load() or {}
        for raw in (data.get("devices") or {}).values():
            try:
                device = RFDeviceConfig.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                _LOGGER.exception("Skipping corrupt device record: %s", raw)
                continue
            self.devices[device.id] = device
        for raw in (data.get("codes") or {}).values():
            try:
                code = RFCode.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                _LOGGER.exception("Skipping corrupt code record: %s", raw)
                continue
            self.codes[code.id] = code
        _LOGGER.debug(
            "Loaded %d devices and %d codes", len(self.devices), len(self.codes)
        )

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        """Return the serialized store payload."""
        return {
            "devices": {
                device_id: device.to_dict()
                for device_id, device in self.devices.items()
            },
            "codes": {code_id: code.to_dict() for code_id, code in self.codes.items()},
        }

    @callback
    def async_schedule_save(self) -> None:
        """Schedule a debounced save to disk."""
        self._store.async_delay_save(self._data_to_save, STORAGE_SAVE_DELAY)

    async def async_save(self) -> None:
        """Persist immediately (used around destructive operations)."""
        await self._store.async_save(self._data_to_save())

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    @callback
    def async_get_device(self, device_id: str) -> RFDeviceConfig | None:
        """Return a device by id."""
        return self.devices.get(device_id)

    @callback
    def async_upsert_device(self, device: RFDeviceConfig) -> None:
        """Create or update a device and schedule a save."""
        self.devices[device.id] = device
        self.async_schedule_save()

    async def async_delete_device(self, device_id: str) -> None:
        """Delete a device including all of its codes and persist."""
        self.devices.pop(device_id, None)
        for code_id in [
            code_id
            for code_id, code in self.codes.items()
            if code.device_id == device_id
        ]:
            self.codes.pop(code_id, None)
        await self.async_save()

    # ------------------------------------------------------------------
    # Codes
    # ------------------------------------------------------------------

    @callback
    def async_get_code(self, code_id: str) -> RFCode | None:
        """Return a code by id."""
        return self.codes.get(code_id)

    @callback
    def async_codes_for_device(self, device_id: str) -> dict[str, RFCode]:
        """Return all codes belonging to a device, keyed by code id."""
        return {
            code_id: code
            for code_id, code in self.codes.items()
            if code.device_id == device_id
        }

    @callback
    def async_upsert_code(self, code: RFCode) -> None:
        """Create or update a code and schedule a save."""
        self.codes[code.id] = code
        self.async_schedule_save()

    @callback
    def async_delete_code(self, code_id: str) -> RFCode | None:
        """Delete a code, schedule a save and return the removed code."""
        code = self.codes.pop(code_id, None)
        if code is not None:
            self.async_schedule_save()
        return code

    @callback
    def async_duplicate_codes(self, source_device_id: str, new_device_id: str) -> int:
        """Clone all codes of one device onto another; returns the count."""
        clones = [
            code.duplicate(new_device_id)
            for code in self.codes.values()
            if code.device_id == source_device_id
        ]
        for clone in clones:
            self.codes[clone.id] = clone
        if clones:
            self.async_schedule_save()
        return len(clones)
