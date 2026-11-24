"""Switch platform for RF Device Manager.

Optimistic (assumed-state) switches: ``on`` sends the on command, ``off``
sends the off command - or the on command again for toggle-style remotes
without a dedicated off code. For those, a received ``on`` flips the state
and a command for the state the switch already has is not sent.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from homeassistant.core import callback

from .const import CONF_DEVICE_CLASS, ENTITY_TYPE_SWITCH
from .coordinator import RFDeviceCoordinator, RFDeviceManagerConfigEntry
from .entity_factory import RFCodeEntity, async_setup_code_platform
from .models import CapturedSignal, RFCode, SLOT_OFF, SLOT_ON

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RF switches for the config entry."""
    async_setup_code_platform(
        hass, entry, async_add_entities, ENTITY_TYPE_SWITCH, RFSwitch
    )


class RFSwitch(RFCodeEntity, SwitchEntity):
    """An RF code exposed as a switch."""

    _attr_is_on = False

    def __init__(self, coordinator: RFDeviceCoordinator, code: RFCode) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, code)
        if device_class := code.settings.get(CONF_DEVICE_CLASS):
            try:
                self._attr_device_class = SwitchDeviceClass(device_class)
            except ValueError:
                self._attr_device_class = None

    async def async_added_to_hass(self) -> None:
        """Restore the optimistic state."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == STATE_ON

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send the on command (optimistic state first for instant UI)."""
        was_on = self._attr_is_on
        self._attr_is_on = True
        self.async_write_ha_state()
        if not (was_on and self._on_toggles_power()):
            await self._async_send_slot(SLOT_ON)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send the off command (falls back to the on/toggle command)."""
        was_on = self._attr_is_on
        self._attr_is_on = False
        self.async_write_ha_state()
        if not self._on_toggles_power():
            await self._async_send_slot(SLOT_OFF)
        elif was_on:  # a toggle press while off would switch it on
            await self._async_send_slot(SLOT_ON)

    @callback
    def _on_rf_match(self, slot: str, signal: CapturedSignal) -> None:
        """Reflect an external remote press in the switch state."""
        if slot == SLOT_ON:
            # A toggle-only power button flips the switch on every press.
            self._attr_is_on = (
                not self._attr_is_on if self._on_toggles_power() else True
            )
        elif slot == SLOT_OFF:
            self._attr_is_on = False
        else:
            return
        self.async_write_ha_state()
