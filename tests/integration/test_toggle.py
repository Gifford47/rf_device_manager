"""Toggle-only power buttons (issue #8): one ``on`` code, no ``off`` code."""

from __future__ import annotations

import pytest

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

from .conftest import (
    code_dict,
    command_dict,
    entity_id_for,
    p8_timings,
    receive,
    sent_hex,
    setup_device,
)

POWER = "0xF0F0F0"
OFF = "0xC3C3C3"


def _power_code(entity_type: str, *, with_off: bool = False) -> dict:
    commands = {"on": [command_dict(p8_timings(POWER))]}
    if with_off:
        commands["off"] = [command_dict(p8_timings(OFF))]
    return {"power": code_dict("power", "Power", entity_type, commands)}


async def _call(hass: HomeAssistant, platform: str, service: str, entity_id: str) -> None:
    await hass.services.async_call(
        platform, service, {"entity_id": entity_id}, blocking=True
    )


@pytest.mark.parametrize("platform", ["light", "switch"])
async def test_received_toggle_press_alternates_the_state(
    hass: HomeAssistant, hass_storage, transmitter, sent, no_receive_guards, platform
) -> None:
    """Every press of a toggle-only power button flips the state."""
    await setup_device(hass, hass_storage, _power_code(platform))
    entity_id = entity_id_for(hass, platform, "power")
    assert hass.states.get(entity_id).state == STATE_OFF

    states = []
    for _ in range(3):
        receive(hass, p8_timings(POWER))
        await hass.async_block_till_done()
        states.append(hass.states.get(entity_id).state)

    assert states == [STATE_ON, STATE_OFF, STATE_ON]
    assert sent == []


@pytest.mark.parametrize("platform", ["light", "switch"])
async def test_toggle_press_is_not_sent_for_the_current_state(
    hass: HomeAssistant, hass_storage, transmitter, sent, platform
) -> None:
    """Sending the toggle for an unchanged state would invert the device."""
    await setup_device(hass, hass_storage, _power_code(platform))
    entity_id = entity_id_for(hass, platform, "power")

    await _call(hass, platform, "turn_off", entity_id)  # already off
    assert sent == []

    await _call(hass, platform, "turn_on", entity_id)
    await _call(hass, platform, "turn_on", entity_id)  # already on
    assert [sent_hex(timings) for timings in sent] == [POWER]

    await _call(hass, platform, "turn_off", entity_id)
    assert [sent_hex(timings) for timings in sent] == [POWER, POWER]
    assert hass.states.get(entity_id).state == STATE_OFF


@pytest.mark.parametrize("platform", ["light", "switch"])
async def test_separate_on_and_off_codes_stay_absolute(
    hass: HomeAssistant, hass_storage, transmitter, sent, no_receive_guards, platform
) -> None:
    """With an ``off`` code, ``on`` keeps meaning on, and off is always sent."""
    await setup_device(hass, hass_storage, _power_code(platform, with_off=True))
    entity_id = entity_id_for(hass, platform, "power")

    receive(hass, p8_timings(POWER))
    receive(hass, p8_timings(POWER))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON

    receive(hass, p8_timings(OFF))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_OFF

    await _call(hass, platform, "turn_off", entity_id)  # already off: resend
    assert [sent_hex(timings) for timings in sent] == [OFF]
