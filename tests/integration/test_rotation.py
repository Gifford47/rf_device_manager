"""Command-variant rotation end to end: send, receive, persist, reset."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from custom_components.rf_device_manager.const import (
    DOMAIN,
    EVENT_CODE_RECEIVED,
    ROTATION_STORAGE_KEY,
)
from custom_components.rf_device_manager.device_manager import get_domain_data

from .conftest import (
    DEVICE_ID,
    GIOLA_SPEED_1,
    SYNTHETIC_SPEED_2,
    TRANSMITTER,
    code_dict,
    command_dict,
    device_options,
    entity_id_for,
    p8_timings,
    receive,
    sent_hex,
    setup_device,
)


def _button(code_id: str, name: str, codes: list[str]) -> dict[str, Any]:
    return code_dict(
        code_id,
        name,
        "button",
        {"press": [command_dict(p8_timings(code)) for code in codes]},
    )


async def _press(hass: HomeAssistant, entity_id: str, times: int = 1) -> None:
    for _ in range(times):
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )


async def _flush_rotation_store(hass: HomeAssistant) -> None:
    """Flush pending delayed saves like Home Assistant does on shutdown."""
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()


async def test_sends_rotate_through_variants_in_learned_order(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """Every press sends the next variant; two consecutive sends never repeat."""
    await setup_device(
        hass, hass_storage, {"speed1": _button("speed1", "Speed 1", GIOLA_SPEED_1)}
    )
    await _press(hass, entity_id_for(hass, "button", "speed1"), times=6)

    assert [sent_hex(timings) for timings in sent] == [
        *GIOLA_SPEED_1,
        *GIOLA_SPEED_1[:2],
    ]
    assert all(a != b for a, b in zip(sent, sent[1:], strict=False))


async def test_rotation_is_persisted_and_restored(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """The next variant survives a restart (issue #2, point 5)."""
    await setup_device(
        hass, hass_storage, {"speed1": _button("speed1", "Speed 1", GIOLA_SPEED_1)}
    )
    await _press(hass, entity_id_for(hass, "button", "speed1"), times=3)
    await _flush_rotation_store(hass)

    assert hass_storage[ROTATION_STORAGE_KEY]["data"] == {"next": {"speed1": {"press": 3}}}

    # Simulate a restart: a fresh RFManager loads the persisted state.
    rf_manager = get_domain_data(hass).rf_manager
    rf_manager._variant_rotation.load({})
    await rf_manager.async_load_rotation()
    assert rf_manager.next_variant_index("speed1", "press", 4) == 3


async def test_persisted_rotation_is_used_after_setup(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """A stored position is picked up on startup."""
    hass_storage[ROTATION_STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": ROTATION_STORAGE_KEY,
        "data": {"next": {"speed1": {"press": 2}}},
    }
    await setup_device(
        hass, hass_storage, {"speed1": _button("speed1", "Speed 1", GIOLA_SPEED_1)}
    )
    await _press(hass, entity_id_for(hass, "button", "speed1"))

    assert [sent_hex(timings) for timings in sent] == [GIOLA_SPEED_1[2]]


async def test_every_received_variant_is_recognized_and_syncs_next_send(
    hass: HomeAssistant, hass_storage, transmitter, sent, no_receive_guards
) -> None:
    """Each press of the physical remote matches; the next send follows it."""
    await setup_device(
        hass, hass_storage, {"speed1": _button("speed1", "Speed 1", GIOLA_SPEED_1)}
    )
    entity_id = entity_id_for(hass, "button", "speed1")
    events = []
    hass.bus.async_listen(EVENT_CODE_RECEIVED, callback(lambda event: events.append(event)))

    for code in GIOLA_SPEED_1:
        receive(hass, p8_timings(code))
    await hass.async_block_till_done()
    assert len(events) == 4  # before #12 only some presses were recognized

    receive(hass, p8_timings(GIOLA_SPEED_1[1]))
    await _press(hass, entity_id)
    assert sent_hex(sent[-1]) == GIOLA_SPEED_1[2]


async def test_received_variant_fires_only_its_own_button(
    hass: HomeAssistant, hass_storage, transmitter, sent, no_receive_guards
) -> None:
    """Variants of sibling buttons (few bits apart) do not cross-fire (#6)."""
    await setup_device(
        hass,
        hass_storage,
        {
            "speed1": _button("speed1", "Speed 1", GIOLA_SPEED_1),
            "speed2": _button("speed2", "Speed 2", SYNTHETIC_SPEED_2),
        },
    )
    events = []
    hass.bus.async_listen(EVENT_CODE_RECEIVED, callback(lambda event: events.append(event)))
    for code in (*GIOLA_SPEED_1, *SYNTHETIC_SPEED_2):
        receive(hass, p8_timings(code))
    await hass.async_block_till_done()

    entity_ids = [event.data["entity_id"] for event in events]
    speed1 = entity_id_for(hass, "button", "speed1")
    speed2 = entity_id_for(hass, "button", "speed2")
    assert entity_ids == [speed1] * 4 + [speed2] * 4


async def test_without_reset_option_sibling_rotation_is_independent(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """Default: another button does not touch a command's rotation."""
    await setup_device(
        hass,
        hass_storage,
        {
            "speed1": _button("speed1", "Speed 1", GIOLA_SPEED_1),
            "speed2": _button("speed2", "Speed 2", SYNTHETIC_SPEED_2),
        },
    )
    speed1 = entity_id_for(hass, "button", "speed1")
    await _press(hass, speed1, times=2)
    await _press(hass, entity_id_for(hass, "button", "speed2"))
    await _press(hass, speed1)

    assert sent_hex(sent[-1]) == GIOLA_SPEED_1[2]


async def test_reset_option_restarts_other_buttons_on_send(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """Issue #2, point 6: another button restarts this button's sequence."""
    await setup_device(
        hass,
        hass_storage,
        {
            "speed1": _button("speed1", "Speed 1", GIOLA_SPEED_1),
            "speed2": _button("speed2", "Speed 2", SYNTHETIC_SPEED_2),
        },
        options=device_options(rotation_reset_on_other=True),
    )
    speed1 = entity_id_for(hass, "button", "speed1")
    speed2 = entity_id_for(hass, "button", "speed2")
    await _press(hass, speed1, times=2)
    await _press(hass, speed2, times=2)
    await _press(hass, speed1)

    assert [sent_hex(timings) for timings in sent] == [
        GIOLA_SPEED_1[0],
        GIOLA_SPEED_1[1],
        SYNTHETIC_SPEED_2[0],
        SYNTHETIC_SPEED_2[1],  # the active button keeps rotating
        GIOLA_SPEED_1[0],  # restarted by the Speed 2 presses
    ]


async def test_reset_option_restarts_other_buttons_on_receive(
    hass: HomeAssistant, hass_storage, transmitter, sent, no_receive_guards
) -> None:
    """A physical press of another button also restarts the sequence."""
    await setup_device(
        hass,
        hass_storage,
        {
            "speed1": _button("speed1", "Speed 1", GIOLA_SPEED_1),
            "speed2": _button("speed2", "Speed 2", SYNTHETIC_SPEED_2),
        },
        options=device_options(rotation_reset_on_other=True),
    )
    speed1 = entity_id_for(hass, "button", "speed1")
    await _press(hass, speed1, times=3)
    receive(hass, p8_timings(SYNTHETIC_SPEED_2[0]))
    await hass.async_block_till_done()
    await _press(hass, speed1)

    assert sent_hex(sent[-1]) == GIOLA_SPEED_1[0]


async def test_reset_option_also_covers_slots_of_one_entity(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """For a fan entity, another speed slot restarts this slot's sequence."""
    fan = code_dict(
        "fan",
        "Ceiling fan",
        "fan",
        {
            "off": [command_dict(p8_timings("0x0F0F0F"))],
            "speed_1": [command_dict(p8_timings(code)) for code in GIOLA_SPEED_1],
            "speed_2": [command_dict(p8_timings(code)) for code in SYNTHETIC_SPEED_2],
        },
        settings={"repeat": 7, "speed_count": 2},
    )
    await setup_device(
        hass,
        hass_storage,
        {"fan": fan},
        options=device_options(rotation_reset_on_other=True),
    )
    entity_id = entity_id_for(hass, "fan", "fan")

    async def set_pct(pct: int) -> None:
        await hass.services.async_call(
            "fan",
            "set_percentage",
            {"entity_id": entity_id, "percentage": pct},
            blocking=True,
        )

    await set_pct(50)
    await set_pct(50)
    await set_pct(100)
    await set_pct(50)

    assert [sent_hex(timings) for timings in sent] == [
        GIOLA_SPEED_1[0],
        GIOLA_SPEED_1[1],
        SYNTHETIC_SPEED_2[0],
        GIOLA_SPEED_1[0],
    ]


async def _send_code(hass: HomeAssistant, code: str) -> None:
    await hass.services.async_call(
        DOMAIN,
        "send_code",
        {"device_id": DEVICE_ID, "code": code},
        blocking=True,
    )


async def test_send_to_unavailable_transmitter_does_not_skip_a_variant(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """A variant that never went on air is sent again by the next send."""
    await setup_device(
        hass, hass_storage, {"speed1": _button("speed1", "Speed 1", GIOLA_SPEED_1)}
    )
    await _send_code(hass, "speed1")
    hass.states.async_set(TRANSMITTER, STATE_UNAVAILABLE)
    with pytest.raises(HomeAssistantError):
        await _send_code(hass, "speed1")
    hass.states.async_set(TRANSMITTER, "unknown")
    await _send_code(hass, "speed1")

    assert [sent_hex(timings) for timings in sent] == GIOLA_SPEED_1[:2]


async def test_failed_transmission_does_not_skip_a_variant_or_reset_siblings(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """A transmitter error keeps this rotation and the siblings' rotations."""
    await setup_device(
        hass,
        hass_storage,
        {
            "speed1": _button("speed1", "Speed 1", GIOLA_SPEED_1),
            "speed2": _button("speed2", "Speed 2", SYNTHETIC_SPEED_2),
        },
        options=device_options(rotation_reset_on_other=True),
    )
    await _press(hass, entity_id_for(hass, "button", "speed1"))
    await _press(hass, entity_id_for(hass, "button", "speed2"))
    with (
        patch(
            "custom_components.rf_device_manager.rf_manager.rf_async_send_command",
            AsyncMock(side_effect=HomeAssistantError("radio busy")),
        ),
        pytest.raises(HomeAssistantError),
    ):
        await _send_code(hass, "speed1")
    await _send_code(hass, "speed2")

    assert [sent_hex(timings) for timings in sent] == [
        GIOLA_SPEED_1[0],
        SYNTHETIC_SPEED_2[0],
        SYNTHETIC_SPEED_2[1],  # not restarted by the failed Speed 1 send
    ]
    rf_manager = get_domain_data(hass).rf_manager
    assert rf_manager.next_variant_index("speed1", "press", 4) == 0


async def test_deleting_a_code_drops_its_rotation(
    hass: HomeAssistant, hass_storage, transmitter, sent
) -> None:
    """No stale rotation state is kept for deleted codes."""
    await setup_device(
        hass, hass_storage, {"speed1": _button("speed1", "Speed 1", GIOLA_SPEED_1)}
    )
    await _press(hass, entity_id_for(hass, "button", "speed1"), times=2)
    get_domain_data(hass).device_manager.async_delete_code("speed1")
    await _flush_rotation_store(hass)

    assert hass_storage[ROTATION_STORAGE_KEY]["data"] == {"next": {}}
