"""Storage migrations (1.1 -> 1.2 -> 1.3) and decoder-revision re-decoding."""

from __future__ import annotations

from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import HomeAssistant, callback

from custom_components.rf_device_manager import rf_analysis
from custom_components.rf_device_manager.const import (
    CONF_DEFAULT_REPEAT,
    CONF_DEVICE_ID,
    DOMAIN,
    EVENT_CODE_RECEIVED,
    STORAGE_KEY,
)
from custom_components.rf_device_manager.device_manager import get_domain_data

from .conftest import (
    DEVICE_ID,
    code_dict,
    command_dict,
    device_options,
    entity_id_for,
    receive,
    setup_device,
)

BITS = "000101000111000100001001"
CURRENT_FINGERPRINT = "24b:fb4b654009f4"
# What the decoders of release 1.1.x stored for the same captures (computed
# with the 1.1 decoders): Protocol 8 with inverted bit polarity, Protocol 4
# without frame splitting at its 2.28 ms sync gap.
LEGACY_P8 = {
    "fingerprint": "24b:655a9c96cac0",
    "bits": "111010111000111011110110",
    "hex": "0xEB8EF6",
}
LEGACY_P4 = {"fingerprint": "399b:c567712f7f08"}


def _legacy_command(protocol: int, legacy: dict[str, Any]) -> dict[str, Any]:
    timings = rf_analysis.encode_binary(BITS, protocol=protocol) * 4
    command = command_dict(timings, repeat=0, fingerprint=legacy["fingerprint"])
    command["extra"] = {
        key: value for key, value in legacy.items() if key != "fingerprint"
    }
    return command


async def _flush(hass: HomeAssistant) -> None:
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()


async def test_migrates_1_1_store_and_redecodes_legacy_codes(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """A 1.1.x store loads, migrates and keeps matching received signals."""
    codes = {
        "p8": code_dict(
            "p8",
            "P8 plug",
            "switch",
            # 1.1 layout: one command object per slot, not a list.
            {"on": _legacy_command(8, LEGACY_P8)},
            settings={"repeat": 0, "has_off": False},
        ),
        "p4": code_dict(
            "p4",
            "P4 button",
            "button",
            {"press": _legacy_command(4, LEGACY_P4)},
            settings={"repeat": 0},
        ),
    }
    await setup_device(
        hass,
        hass_storage,
        codes,
        minor_version=1,
        decoder_revision=None,  # 1.1 stores carry no decoder revision
        device_settings={"default_repeat": 0},
    )
    await _flush(hass)

    stored = hass_storage[STORAGE_KEY]
    assert (stored["version"], stored["minor_version"]) == (1, 3)
    data = stored["data"]
    assert data["decoder_revision"] == rf_analysis.DECODER_REVISION
    assert data["devices"][DEVICE_ID]["settings"]["default_repeat"] == 7

    p8 = data["codes"]["p8"]
    assert p8["settings"]["repeat"] == 7  # 1.1 -> 1.2: repeat 0 meant 7
    assert isinstance(p8["rf_data"]["commands"]["on"], list)  # 1.2 -> 1.3
    p8_on = p8["rf_data"]["commands"]["on"][0]
    assert p8_on["repeat"] == 7
    assert p8_on["fingerprint"] == CURRENT_FINGERPRINT  # re-decoded
    assert p8_on["extra"]["bits"] == BITS
    p4_press = data["codes"]["p4"]["rf_data"]["commands"]["press"][0]
    assert p4_press["fingerprint"] == CURRENT_FINGERPRINT

    # The original timings are untouched - sending is exactly as before.
    original = rf_analysis.normalize(rf_analysis.encode_binary(BITS, protocol=8) * 4)
    assert p8_on["data"] == original

    # And the point of re-decoding: the physical remote is recognized again.
    events = []
    hass.bus.async_listen(
        EVENT_CODE_RECEIVED, callback(lambda event: events.append(event))
    )
    receive(hass, rf_analysis.encode_binary(BITS, protocol=8) * 4)
    receive(hass, rf_analysis.encode_binary(BITS, protocol=4) * 4)
    await hass.async_block_till_done()
    assert {event.data["entity_id"] for event in events} == {
        entity_id_for(hass, "switch", "p8"),
        entity_id_for(hass, "button", "p4"),
    }


async def test_current_store_is_not_redecoded(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """Re-decoding only runs when the decoder revision increased."""
    command = _legacy_command(8, LEGACY_P8)
    command["repeat"] = 7
    await setup_device(
        hass,
        hass_storage,
        {"p8": code_dict("p8", "P8", "button", {"press": [command]})},
    )
    code = get_domain_data(hass).store.async_get_code("p8")
    assert code is not None
    assert code.commands["press"][0].fingerprint == LEGACY_P8["fingerprint"]


async def test_store_1_2_with_variant_lists_already_is_accepted(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """Data written by the pre-release #12 branch (1.2 + lists) still loads."""
    variants = [
        command_dict(rf_analysis.encode_binary(BITS, protocol=1) * 4),
        command_dict(rf_analysis.encode_binary("1" * 24, protocol=1) * 4),
    ]
    await setup_device(
        hass,
        hass_storage,
        {"b": code_dict("b", "B", "button", {"press": variants})},
        minor_version=2,
    )
    code = get_domain_data(hass).store.async_get_code("b")
    assert code is not None
    assert len(code.commands["press"]) == 2


async def test_config_entry_1_1_repeat_zero_is_migrated(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """Config entry 1.1 -> 1.2 rewrites a default repeat of 0 to 7 (#4)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Old device",
        data={CONF_DEVICE_ID: DEVICE_ID},
        options=device_options(**{CONF_DEFAULT_REPEAT: 0}),
        unique_id=DEVICE_ID,
        version=1,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.minor_version == 2
    assert entry.options[CONF_DEFAULT_REPEAT] == 7
