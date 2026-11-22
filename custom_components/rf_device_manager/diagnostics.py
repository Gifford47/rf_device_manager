"""Diagnostics support for RF Device Manager."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import RFDeviceManagerConfigEntry
from .device_manager import get_domain_data
from .rf_manager import async_list_transmitters

_TIMING_PREVIEW = 24


def _redact_command(command: dict[str, Any]) -> dict[str, Any]:
    """Shorten timing arrays so diagnostics stay readable."""
    data = command.get("data", [])
    return {
        **command,
        "data": data[:_TIMING_PREVIEW],
        "data_length": len(data),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RFDeviceManagerConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    domain_data = get_domain_data(hass)
    await domain_data.rf_manager.hub.async_discover()
    device_id = entry.data.get("device_id", "")
    device = domain_data.store.async_get_device(device_id)
    codes = domain_data.store.async_codes_for_device(device_id)

    return {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "device": device.to_dict() if device else None,
        "codes": [
            {
                **code.to_dict(),
                "rf_data": {
                    "commands": {
                        slot: [_redact_command(command.to_dict()) for command in variants]
                        for slot, variants in code.commands.items()
                    }
                },
            }
            for code in codes.values()
        ],
        "transmitters": [
            {
                "entity_id": info.entity_id,
                "name": info.name,
                "available": info.available,
            }
            for info in async_list_transmitters(hass)
        ],
        "receivers": domain_data.rf_manager.hub.async_receiver_names(),
    }
