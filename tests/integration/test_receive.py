"""Receiving: matcher registration lifecycle and the bus-event capture source."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback

from custom_components.rf_device_manager.const import (
    ESPHOME_FALLBACK_EVENT,
    EVENT_CODE_RECEIVED,
)
from custom_components.rf_device_manager.device_manager import get_domain_data

from .conftest import (
    GIOLA_SPEED_1,
    code_dict,
    command_dict,
    p8_timings,
    setup_device,
)


def _single_button() -> dict:
    return {
        "speed1": code_dict(
            "speed1",
            "Speed 1",
            "button",
            {"press": [command_dict(p8_timings(code)) for code in GIOLA_SPEED_1]},
        )
    }


async def test_code_edit_does_not_restart_the_receiver_hub(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """Re-registering the only entity must not stop and restart the hub."""
    await setup_device(hass, hass_storage, _single_button())
    domain_data = get_domain_data(hass)
    hub = domain_data.rf_manager.hub
    generation = hub._generation
    assert hub._subscribers

    code = domain_data.store.async_get_code("speed1")
    assert code is not None
    code.name = "Speed one"
    domain_data.device_manager.async_update_code(code)
    await hass.async_block_till_done()

    assert hub._generation == generation
    assert hub._subscribers
    # The new registration replaced the old one instead of adding to it.
    assert len(domain_data.rf_manager.matcher._entries) == len(GIOLA_SPEED_1)


async def test_bus_event_with_unparsable_frequency_is_still_matched(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """A malformed ``frequency`` must not drop an otherwise valid capture."""
    await setup_device(hass, hass_storage, _single_button())
    events = []
    hass.bus.async_listen(EVENT_CODE_RECEIVED, callback(lambda event: events.append(event)))

    hass.bus.async_fire(
        ESPHOME_FALLBACK_EVENT,
        {"timings": p8_timings(GIOLA_SPEED_1[0]), "frequency": "433.92 MHz"},
    )
    await hass.async_block_till_done()

    assert len(events) == 1
