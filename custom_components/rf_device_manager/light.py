"""Light platform for RF Device Manager.

Optimistic RF lights with optional stepped brightness: many RF remotes only
offer brighter/dimmer buttons, so absolute brightness requests are mapped
to a number of up/down command repetitions.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_BRIGHTNESS_STEPS, ENTITY_TYPE_LIGHT
from .coordinator import RFDeviceCoordinator, RFDeviceManagerConfigEntry
from .entity_factory import RFCodeEntity, async_setup_code_platform
from .models import (
    CapturedSignal,
    RFCode,
    SLOT_BRIGHTNESS_DOWN,
    SLOT_BRIGHTNESS_UP,
    SLOT_OFF,
    SLOT_ON,
)

PARALLEL_UPDATES = 1

_STEP_DELAY = 0.15  # seconds between repeated brightness commands


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RF lights for the config entry."""
    async_setup_code_platform(
        hass, entry, async_add_entities, ENTITY_TYPE_LIGHT, RFLight
    )


class RFLight(RFCodeEntity, LightEntity):
    """An RF code exposed as a light."""

    _attr_is_on = False

    def __init__(self, coordinator: RFDeviceCoordinator, code: RFCode) -> None:
        """Initialize the light."""
        super().__init__(coordinator, code)
        if self._has_brightness_commands(code):
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_brightness = 255
        else:
            self._attr_color_mode = ColorMode.ONOFF
            self._attr_supported_color_modes = {ColorMode.ONOFF}

    @staticmethod
    def _has_brightness_commands(code: RFCode) -> bool:
        """Return whether up and down brightness commands are configured."""
        return (
            SLOT_BRIGHTNESS_UP in code.commands
            and SLOT_BRIGHTNESS_DOWN in code.commands
        )

    async def async_added_to_hass(self) -> None:
        """Restore the optimistic state."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == STATE_ON
            if (brightness := last_state.attributes.get(ATTR_BRIGHTNESS)) is not None:
                self._attr_brightness = brightness

    async def _async_step_brightness(self, target: int) -> None:
        """Approach a brightness target with repeated up/down commands."""
        code = self.code
        if code is None:
            return
        total_steps = max(2, int(code.settings.get(CONF_BRIGHTNESS_STEPS, 10)))
        step_size = 255 / total_steps
        current = self._attr_brightness if self._attr_brightness is not None else 255
        delta_steps = round((target - current) / step_size)
        if delta_steps == 0:
            return
        slot = SLOT_BRIGHTNESS_UP if delta_steps > 0 else SLOT_BRIGHTNESS_DOWN
        for index in range(min(abs(delta_steps), total_steps)):
            if index:
                await asyncio.sleep(_STEP_DELAY)
            await self._async_send_slot(slot)
        self._attr_brightness = target

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on, optionally stepping to a brightness."""
        was_on = self._attr_is_on
        self._attr_is_on = True
        self.async_write_ha_state()  # optimistic state first for instant UI
        if not was_on:
            await self._async_send_slot(SLOT_ON)
        if (
            (brightness := kwargs.get(ATTR_BRIGHTNESS)) is not None
            and self.code is not None
            and self._has_brightness_commands(self.code)
        ):
            await self._async_step_brightness(int(brightness))
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off (falls back to the on/toggle command)."""
        was_on = self._attr_is_on
        self._attr_is_on = False
        self.async_write_ha_state()
        if not self._on_toggles_power():
            await self._async_send_slot(SLOT_OFF)
        elif was_on:  # a toggle press while off would switch it on
            await self._async_send_slot(SLOT_ON)

    @callback
    def _on_rf_match(self, slot: str, signal: CapturedSignal) -> None:
        """Reflect an external remote press in the light state."""
        if slot == SLOT_ON:
            # A toggle-only power button flips the light on every press.
            self._attr_is_on = (
                not self._attr_is_on if self._on_toggles_power() else True
            )
        elif slot == SLOT_OFF:
            self._attr_is_on = False
        else:
            return
        self.async_write_ha_state()
