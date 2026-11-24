"""Services: ``send_code`` respects disabled codes and devices."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.rf_device_manager.const import DOMAIN

from .conftest import (
    DEVICE_ID,
    code_dict,
    command_dict,
    device_options,
    p8_timings,
    setup_device,
)


def _button(enabled: bool = True) -> dict:
    code = code_dict(
        "light",
        "Light",
        "button",
        {"press": [command_dict(p8_timings("0x0F0F0F"))]},
    )
    code["enabled"] = enabled
    return {"light": code}


async def _send_code(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        DOMAIN,
        "send_code",
        {"device_id": DEVICE_ID, "code": "Light"},
        blocking=True,
    )


async def test_send_code_sends_an_enabled_code(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    await setup_device(hass, hass_storage, _button())
    await _send_code(hass)
    assert len(sent) == 1


async def test_send_code_refuses_a_disabled_code(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    await setup_device(hass, hass_storage, _button(enabled=False))
    with pytest.raises(ServiceValidationError) as err:
        await _send_code(hass)
    assert err.value.translation_key == "code_disabled"
    assert sent == []


async def test_send_code_refuses_a_disabled_device(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    await setup_device(
        hass, hass_storage, _button(), options=device_options(enabled=False)
    )
    with pytest.raises(ServiceValidationError) as err:
        await _send_code(hass)
    assert err.value.translation_key == "device_disabled"
    assert sent == []
