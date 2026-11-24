"""Shared fixtures for the Home Assistant integration tests.

These tests run the integration inside a real Home Assistant core (via
``pytest-homeassistant-custom-component``). RF hardware is replaced by:

* a fake ``radio_frequency`` transmitter state (``TRANSMITTER``), and
* a mocked ``rf_async_send_command`` that records every transmitted command.

Received signals are injected with :func:`receive`, which feeds timings into
the receiver hub exactly like an ESPHome receive callback would.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.rf_device_manager import rf_analysis
from custom_components.rf_device_manager.const import (
    CONF_DEFAULT_REPEAT,
    CONF_DESCRIPTION,
    CONF_DEVICE_ID,
    CONF_ENABLED,
    CONF_FREQUENCY,
    CONF_NAME,
    CONF_ROTATION_RESET,
    CONF_TRANSMITTER,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
)
from custom_components.rf_device_manager.device_manager import get_domain_data

TRANSMITTER = "radio_frequency.test_transmitter"
DEVICE_ID = "device1"
FREQUENCY = 433_920_000

# Issue #2: the four codes the Giola fan remote cycles through for "Speed 1"
# (as reported). A second button with the same structure is synthetic - the
# reported "Speed 2" codes were garbled in the issue.
GIOLA_SPEED_1 = ["0x147109", "0x17720A", "0x16730B", "0x157008"]
SYNTHETIC_SPEED_2 = ["0x461190", "0x76219A", "0x66311B", "0x560180"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load ``custom_components/rf_device_manager`` in every test."""


def p8_timings(hex_code: str, frames: int = 4) -> list[int]:
    """Return rc-switch protocol 8 timings for a 24-bit hex code."""
    bits = format(int(hex_code, 16), "024b")
    return rf_analysis.encode_binary(bits, protocol=8) * frames


def command_dict(timings: list[int], **overrides: Any) -> dict[str, Any]:
    """Serialize a learned command the way the learn wizard stores it."""
    analysis = rf_analysis.analyze(timings)
    data = {
        "protocol": "raw",
        "frequency": FREQUENCY,
        "data": rf_analysis.normalize(timings),
        "pulse_length": analysis.get("base_unit_us"),
        "code": None,
        "repeat": 7,
        "source": "learned:test",
        "fingerprint": analysis["fingerprint"],
        "extra": {
            key: analysis[key]
            for key in rf_analysis.STORED_DECODE_KEYS
            if key in analysis
        },
    }
    data.update(overrides)
    return data


def code_dict(
    code_id: str,
    name: str,
    entity_type: str,
    commands: dict[str, Any],
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize an RF code record."""
    return {
        "id": code_id,
        "device_id": DEVICE_ID,
        "name": name,
        "entity_type": entity_type,
        "description": "",
        "enabled": True,
        "rf_data": {"commands": commands},
        "settings": settings or {"repeat": 7},
    }


def device_options(**overrides: Any) -> dict[str, Any]:
    """Config entry options of the test device."""
    options = {
        CONF_NAME: "Fan remote",
        CONF_DESCRIPTION: "",
        CONF_TRANSMITTER: TRANSMITTER,
        CONF_FREQUENCY: FREQUENCY,
        CONF_DEFAULT_REPEAT: 7,
        CONF_ROTATION_RESET: False,
        CONF_ENABLED: True,
    }
    options.update(overrides)
    return options


def store_payload(
    codes: dict[str, Any],
    *,
    minor_version: int = STORAGE_MINOR_VERSION,
    decoder_revision: int | None = rf_analysis.DECODER_REVISION,
    device_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``hass_storage`` entry for the main store."""
    data: dict[str, Any] = {
        "devices": {
            DEVICE_ID: {
                "id": DEVICE_ID,
                "name": "Fan remote",
                "transmitter": TRANSMITTER,
                "settings": {
                    "description": "",
                    "frequency": FREQUENCY,
                    "default_repeat": 7,
                    "enabled": True,
                    **(device_settings or {}),
                },
            }
        },
        "codes": codes,
    }
    if decoder_revision is not None:
        data["decoder_revision"] = decoder_revision
    return {
        "version": STORAGE_VERSION,
        "minor_version": minor_version,
        "key": STORAGE_KEY,
        "data": data,
    }


@pytest.fixture
def transmitter(hass: HomeAssistant) -> str:
    """Provide a ready (``unknown``) radio_frequency transmitter state."""
    hass.states.async_set(TRANSMITTER, "unknown", {"friendly_name": "Test TX"})
    return TRANSMITTER


@pytest.fixture
def sent() -> Generator[list[list[int]]]:
    """Record the raw timings of every transmitted command."""
    timings: list[list[int]] = []

    async def _send(hass: HomeAssistant, entity_id: str, command: Any) -> None:
        timings.append(list(command.get_raw_timings()))

    with (
        patch(
            "custom_components.rf_device_manager.rf_manager.rf_async_send_command",
            AsyncMock(side_effect=_send),
        ),
        patch(
            "custom_components.rf_device_manager.rf_manager.rf_async_get_transmitters",
            return_value=[TRANSMITTER],
        ),
    ):
        yield timings


@pytest.fixture
def no_receive_guards() -> Generator[None]:
    """Disable self-echo suppression and per-slot receive debounce.

    Both use the real monotonic loop clock; tests inject receptions back to
    back, so they would otherwise be swallowed.
    """
    with (
        patch(
            "custom_components.rf_device_manager.entity_factory.SELF_ECHO_SUPPRESS_S",
            0.0,
        ),
        patch(
            "custom_components.rf_device_manager.entity_factory.RECV_DEBOUNCE_S",
            0.0,
        ),
    ):
        yield


def sent_hex(timings: list[int]) -> str:
    """Decode a transmitted command back to its hex code."""
    return rf_analysis.analyze(timings)["hex"].upper().replace("0X", "0x")


async def setup_device(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    codes: dict[str, Any],
    *,
    options: dict[str, Any] | None = None,
    **store_kwargs: Any,
) -> MockConfigEntry:
    """Pre-fill the store, add the device entry and set it up."""
    hass_storage[STORAGE_KEY] = store_payload(codes, **store_kwargs)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Fan remote",
        data={CONF_DEVICE_ID: DEVICE_ID},
        options=options or device_options(),
        unique_id=DEVICE_ID,
        version=1,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity_id_for(hass: HomeAssistant, platform: str, code_id: str) -> str:
    """Return the entity id of a code's entity."""
    from homeassistant.helpers import entity_registry as er

    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{DEVICE_ID}_{code_id}"
    )
    assert entity_id is not None
    return entity_id


def receive(hass: HomeAssistant, timings: list[int]) -> None:
    """Inject a reception as if an ESPHome receiver had captured it."""
    get_domain_data(hass).rf_manager.hub._async_dispatch(timings, "test", None)
