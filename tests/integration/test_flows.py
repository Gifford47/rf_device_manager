"""Config/options flows: device option, learning variants, manual variants, bulk."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.rf_device_manager import rf_analysis
from custom_components.rf_device_manager.config_flow import (
    _HAS_CONDITIONAL_FORM_FIELDS,
)
from custom_components.rf_device_manager.const import (
    CONF_ROTATION_RESET,
    DOMAIN,
)
from custom_components.rf_device_manager.device_manager import get_domain_data
from custom_components.rf_device_manager.rf_manager import LearnSession

from .conftest import (
    DEVICE_ID,
    GIOLA_SPEED_1,
    TRANSMITTER,
    p8_timings,
    receive,
    sent_hex,
    setup_device,
)

LEARN_WINDOW = 0.2  # seconds; the real wizard collects for 10 s


@contextmanager
def fake_receiver(hass: HomeAssistant) -> Iterator[None]:
    """Pretend one ESPHome RF receiver exists and shorten learn sessions.

    Receptions are injected with ``receive()`` and go through the real
    ``LearnSession`` (dedup, clustering, ranking) and the real hub.
    """
    rf_manager = get_domain_data(hass).rf_manager

    def _session(timeout: float = 5.0) -> LearnSession:
        return LearnSession(
            hub=rf_manager.hub, timeout=5.0, collect_window=LEARN_WINDOW
        )

    with (
        patch.object(rf_manager.hub, "async_discover", AsyncMock(return_value=None)),
        patch.object(
            rf_manager.hub, "async_receiver_names", return_value=["Test receiver"]
        ),
        patch.object(rf_manager, "new_learn_session", side_effect=_session),
    ):
        yield


async def _menu(hass: HomeAssistant, flow_id: str, option: str) -> ConfigFlowResult:
    return await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": option}
    )


async def _learn(
    hass: HomeAssistant, result: ConfigFlowResult, presses: list[list[int]]
) -> ConfigFlowResult:
    """Drive a ``slot_learn`` progress step with the given receptions."""
    assert result["type"] is FlowResultType.SHOW_PROGRESS, result
    for timings in presses:
        receive(hass, timings)
    await asyncio.sleep(LEARN_WINDOW * 2)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_configure(result["flow_id"])
    if result["type"] is FlowResultType.SHOW_PROGRESS_DONE:
        result = await hass.config_entries.options.async_configure(result["flow_id"])
    return result


def _schema_default(result: ConfigFlowResult, key: str) -> Any:
    for marker in result["data_schema"].schema:
        if str(marker) == key:
            return marker.default()
    raise KeyError(key)


def _four_presses(frames_per_press: int) -> list[list[int]]:
    """Four presses of the rotating 'Speed 1' button, plus one noise burst."""
    presses: list[list[int]] = []
    for index, code in enumerate(GIOLA_SPEED_1):
        presses.extend([p8_timings(code, frames=2)] * frames_per_press)
        if index == 1:
            presses.append([300, -900, 1200, -400, 700, -2500, 150, -150, 900, -300])
    return presses


async def test_create_device_with_rotation_reset_option(
    hass: HomeAssistant, transmitter, sent
) -> None:
    """The new device option is offered, stored and mirrored to the store."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["step_id"] == "create_device"
    assert CONF_ROTATION_RESET in {str(key) for key in result["data_schema"].schema}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Ceiling fan",
            "transmitter": TRANSMITTER,
            "frequency_preset": "433920000",
            "default_repeat": 7,
            CONF_ROTATION_RESET: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_ROTATION_RESET] is True
    await hass.async_block_till_done()

    store = get_domain_data(hass).store
    device = store.async_get_device(result["result"].data["device_id"])
    assert device is not None
    assert device.settings[CONF_ROTATION_RESET] is True


@pytest.mark.parametrize("frames_per_press", [3, 1])
async def test_learn_rotating_button_preselects_all_variants(
    hass: HomeAssistant, hass_storage, transmitter, sent, frames_per_press
) -> None:
    """Four presses -> four variants in press order, preselected, saved.

    ``frames_per_press=1`` models a receiver that delivers one capture per
    press (every candidate is seen once).
    """
    entry = await setup_device(hass, hass_storage, {})
    with fake_receiver(hass):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await _menu(hass, result["flow_id"], "add_code")
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"name": "Speed 1", "entity_type": "button"}
        )
        assert result["step_id"] == "slot_menu"
        result = await _menu(hass, result["flow_id"], "slot_learn")
        result = await _learn(hass, result, _four_presses(frames_per_press))

        assert result["step_id"] == "slot_learn_done"
        default = _schema_default(result, "signals")
        assert len(default) == 4, default  # noise is not preselected

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"signals": default}
        )
        assert result["step_id"] == "slot_confirm"
        assert result["description_placeholders"]["variant_count"] == "4"
        result = await _menu(hass, result["flow_id"], "slot_accept")
        assert result["step_id"] == "save_code"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY

    codes = get_domain_data(hass).store.async_codes_for_device(DEVICE_ID)
    (code,) = codes.values()
    learned = [
        rf_analysis.analyze(command.data)["hex"].upper().replace("0X", "0x")
        for command in code.commands["press"]
    ]
    assert learned == GIOLA_SPEED_1


@pytest.mark.skipif(
    _HAS_CONDITIONAL_FORM_FIELDS, reason="Core >= 2026.11 uses the one-page form"
)
async def test_manual_variants_legacy_two_step_form(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """Core < 2026.11: format first, then the format's variant list."""
    entry = await setup_device(hass, hass_storage, {})
    rf_manager = get_domain_data(hass).rf_manager
    with patch.object(rf_manager.hub, "async_discover", AsyncMock(return_value=None)):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await _menu(hass, result["flow_id"], "add_code")
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"name": "Speed 1", "entity_type": "button"}
        )
        result = await _menu(hass, result["flow_id"], "slot_manual")
        assert result["step_id"] == "slot_manual"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"input_format": "binary"}
        )
        assert result["step_id"] == "slot_manual_data"
        keys = {str(key) for key in result["data_schema"].schema}
        assert {"variants_binary", "pulse_length", "rc_protocol", "repeat"} <= keys

        # An empty variant is rejected with a helpful error.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"variants_binary": [{"value": " "}], "rc_protocol": "8"},
        )
        assert result["errors"] == {"base": "manual_variant_required"}

        variants = [
            {"value": format(int(code, 16), "024b")} for code in GIOLA_SPEED_1
        ]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"variants_binary": variants, "rc_protocol": "8", "repeat": 3},
        )
        assert result["step_id"] == "slot_confirm"
        assert result["description_placeholders"]["variant_count"] == "4"
        result = await _menu(hass, result["flow_id"], "slot_accept")
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY

    (code,) = get_domain_data(hass).store.async_codes_for_device(DEVICE_ID).values()
    commands = code.commands["press"]
    assert [command.code for command in commands] == [
        variant["value"] for variant in variants
    ]
    assert {command.repeat for command in commands} == {3}

    # The manual variants rotate on send as well.
    from homeassistant.helpers import entity_registry as er

    entity_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, code.unique_id
    )
    for _ in range(2):
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
    assert [sent_hex(timings) for timings in sent] == GIOLA_SPEED_1[:2]


async def test_bulk_buttons_learn_variants_and_warn_on_duplicates(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """#7 + #12: bulk wizard stores variant lists; re-pressing warns."""
    entry = await setup_device(hass, hass_storage, {})
    with fake_receiver(hass):
        await _bulk_flow(hass, entry)

    codes = get_domain_data(hass).store.async_codes_for_device(DEVICE_ID)
    by_name = {code.name: code for code in codes.values()}
    assert len(by_name["Speed 1"].commands["press"]) == 4
    assert len(by_name["Speed 2"].commands["press"]) == 1


async def _bulk_flow(hass: HomeAssistant, entry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await _menu(hass, result["flow_id"], "add_buttons")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"button_names": "Speed 1, Speed 2"}
    )
    result = await _learn(hass, result, _four_presses(3))
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"signals": _schema_default(result, "signals")}
    )
    assert result["step_id"] == "slot_confirm"
    assert "⚠️" not in result["description_placeholders"]["test_note"]
    result = await _menu(hass, result["flow_id"], "slot_accept")

    # Second button: the user presses "Speed 1" again by mistake - only its
    # third variant. The warning must catch a match against *any* variant.
    result = await _learn(hass, result, [p8_timings(GIOLA_SPEED_1[2])] * 3)
    if result["step_id"] == "slot_learn_done":
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"signals": _schema_default(result, "signals")}
        )
    assert result["step_id"] == "slot_confirm"
    assert "Speed 1" in result["description_placeholders"]["test_note"]
    result = await _menu(hass, result["flow_id"], "slot_accept")
    assert result["step_id"] == "bulk_done"


async def test_device_settings_round_trip_rotation_option(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """The options flow shows and saves the option."""
    entry = await setup_device(hass, hass_storage, {})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await _menu(hass, result["flow_id"], "device_settings")
    assert CONF_ROTATION_RESET in {str(key) for key in result["data_schema"].schema}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Fan remote",
            "transmitter": TRANSMITTER,
            "frequency_preset": "433920000",
            "default_repeat": 7,
            CONF_ROTATION_RESET: True,
            "enabled": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_ROTATION_RESET] is True
    device = get_domain_data(hass).store.async_get_device(DEVICE_ID)
    assert device is not None and device.settings[CONF_ROTATION_RESET] is True
