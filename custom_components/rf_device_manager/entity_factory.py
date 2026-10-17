"""Entity base classes and dynamic entity creation.

Every stored RF code materializes as exactly one entity on the platform
matching its ``entity_type``. Platforms register through
``async_setup_code_platform`` which adds entities live whenever codes are
created in the options flow (no config entry reload required) and lets
entities remove themselves when their code is deleted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ICON,
    DOMAIN,
    ENTITY_TYPE_BINARY_SENSOR,
    ENTITY_TYPE_BUTTON,
    ENTITY_TYPE_COVER,
    ENTITY_TYPE_FAN,
    ENTITY_TYPE_LIGHT,
    ENTITY_TYPE_NUMBER,
    ENTITY_TYPE_SELECT,
    ENTITY_TYPE_SENSOR,
    ENTITY_TYPE_SWITCH,
    EVENT_CODE_RECEIVED,
    MANUFACTURER,
    MODEL,
    RECEIVE_ONLY_TYPES,
)
from .coordinator import RFDeviceCoordinator, RFDeviceManagerConfigEntry
from .device_manager import get_domain_data
from .models import CapturedSignal, RFCode, RFCommandData

_LOGGER = logging.getLogger(__name__)

# Static per-type entity descriptions (default icons; device classes and
# features are configured per code through the options flow).
ENTITY_DESCRIPTIONS: dict[str, EntityDescription] = {
    ENTITY_TYPE_SWITCH: EntityDescription(key=ENTITY_TYPE_SWITCH),
    ENTITY_TYPE_LIGHT: EntityDescription(key=ENTITY_TYPE_LIGHT),
    ENTITY_TYPE_FAN: EntityDescription(key=ENTITY_TYPE_FAN),
    ENTITY_TYPE_COVER: EntityDescription(key=ENTITY_TYPE_COVER),
    ENTITY_TYPE_BUTTON: EntityDescription(key=ENTITY_TYPE_BUTTON, icon="mdi:remote"),
    ENTITY_TYPE_SELECT: EntityDescription(
        key=ENTITY_TYPE_SELECT, icon="mdi:tune-variant"
    ),
    ENTITY_TYPE_NUMBER: EntityDescription(key=ENTITY_TYPE_NUMBER, icon="mdi:tune"),
    ENTITY_TYPE_SENSOR: EntityDescription(
        key=ENTITY_TYPE_SENSOR, icon="mdi:radio-tower"
    ),
    ENTITY_TYPE_BINARY_SENSOR: EntityDescription(key=ENTITY_TYPE_BINARY_SENSOR),
}

CodeEntityFactory = Callable[[RFDeviceCoordinator, RFCode], Entity]

# Window during which an entity ignores received codes after it transmitted,
# to avoid the receiver echoing our own signal back into the state.
SELF_ECHO_SUPPRESS_S = 2.0

# One remote press is transmitted as many repeated frames; collapse repeats of
# the same slot within this window into a single reception.
RECV_DEBOUNCE_S = 1.0


class RFCodeEntity(CoordinatorEntity[RFDeviceCoordinator], RestoreEntity):
    """Base entity backed by one stored RF code.

    Besides sending, every entity also *listens*: its command fingerprints
    are registered with the shared :class:`CodeMatcher`, so when the same RF
    code is received from outside Home Assistant (e.g. the physical remote),
    ``_on_rf_match`` fires and the entity can reflect the external state
    change. Send-only platforms override ``_on_rf_match`` to update their
    optimistic state; receive-only platforms (sensor, binary_sensor) use it
    as their primary state source.
    """

    _attr_has_entity_name = True
    _attr_assumed_state = True
    _removing = False
    _matcher_unsub: CALLBACK_TYPE | None = None
    # Ignore received matches shortly after our own transmission, so the
    # receiver hearing our own code does not clobber the optimistic state
    # (matters for toggle-style devices whose off == on code).
    _suppress_receive_until: float = 0.0

    def __init__(self, coordinator: RFDeviceCoordinator, code: RFCode) -> None:
        """Initialize from the coordinator snapshot and a code record."""
        super().__init__(coordinator)
        self._code_id = code.id
        self._recv_debounce: dict[str, float] = {}
        self._attr_unique_id = code.unique_id
        self.entity_description = ENTITY_DESCRIPTIONS[code.entity_type]
        device = coordinator.data.device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    # ------------------------------------------------------------------
    # Code / device access
    # ------------------------------------------------------------------

    @property
    def code(self) -> RFCode | None:
        """Return the live code record, or ``None`` after deletion."""
        return self.coordinator.data.codes.get(self._code_id)

    @property
    def rf_device(self):
        """Return the live device config."""
        return self.coordinator.data.device

    # ------------------------------------------------------------------
    # Presentation
    # ------------------------------------------------------------------

    @property
    def name(self) -> str | None:
        """Return the code name (live, so store renames apply instantly)."""
        code = self.code
        return code.name if code else None

    @property
    def icon(self) -> str | None:
        """Return the user icon override or the type default."""
        code = self.code
        if code and (icon := code.settings.get(CONF_ICON)):
            return icon
        return super().icon

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose code metadata for automations and debugging."""
        code = self.code
        if code is None:
            return None
        attributes: dict[str, Any] = {
            "rf_device_id": code.device_id,
            "rf_code_id": code.id,
            # Per-command protocol summary, e.g.
            # "open": "Space distance (EV1527/PT2262-style) · 0x… (63 bit)"
            "commands": {
                slot: command.protocol_summary()
                for slot, command in sorted(code.commands.items())
            },
        }
        if code.description:
            attributes["description"] = code.description
        return attributes

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    @property
    def _requires_transmitter(self) -> bool:
        """Return whether this entity sends RF (vs. receive-only)."""
        code = self.code
        return code is not None and code.entity_type not in RECEIVE_ONLY_TYPES

    @property
    def available(self) -> bool:
        """Available when the code exists/enabled and the radio is usable."""
        if not super().available:
            return False
        code = self.code
        if code is None or not code.enabled or not self.rf_device.enabled:
            return False
        if self._requires_transmitter:
            state = self.hass.states.get(self.rf_device.transmitter)
            return state is not None and state.state != "unavailable"
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Track transmitter availability and register for received codes."""
        await super().async_added_to_hass()
        if self._requires_transmitter and self.rf_device.transmitter:

            @callback
            def _transmitter_changed(
                _event: Event[EventStateChangedData],
            ) -> None:
                self.async_write_ha_state()

            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self.rf_device.transmitter], _transmitter_changed
                )
            )

        self._async_register_matcher()
        self.async_on_remove(self._async_unregister_matcher)

    # ------------------------------------------------------------------
    # Receiving (external state changes via the shared CodeMatcher)
    # ------------------------------------------------------------------

    @callback
    def _async_register_matcher(self) -> None:
        """Register this code's command slots with the shared matcher."""
        code = self.code
        if code is None:
            return
        slots = [
            (slot, command.fingerprint, command.extra.get("bits"))
            for slot, command in code.commands.items()
            if command.fingerprint or command.extra.get("bits")
        ]
        if not slots:
            return
        matcher = get_domain_data(self.hass).rf_manager.matcher
        self._matcher_unsub = matcher.async_register(
            slots, self._handle_received, group=code.device_id
        )

    @callback
    def _async_unregister_matcher(self) -> None:
        """Remove this entity's matcher registrations."""
        if self._matcher_unsub is not None:
            self._matcher_unsub()
            self._matcher_unsub = None

    @callback
    def _handle_received(self, slot: str, signal: CapturedSignal) -> None:
        """Handle an externally received code matching one of our slots."""
        code = self.code
        if code is None or not code.enabled:
            return
        now = self.hass.loop.time()
        if now < self._suppress_receive_until:
            return  # our own transmission echoed back on the receiver
        if now - self._recv_debounce.get(slot, 0.0) < RECV_DEBOUNCE_S:
            return  # a repeated frame of the same press
        self._recv_debounce[slot] = now
        _LOGGER.debug(
            "RF match on %s: slot %s (%s)", self.entity_id, slot, signal.fingerprint
        )
        self.hass.bus.async_fire(
            EVENT_CODE_RECEIVED,
            {
                "entity_id": self.entity_id,
                "rf_device_id": code.device_id,
                "code_id": code.id,
                "code_name": code.name,
                "slot": slot,
                "source": signal.source,
            },
        )
        self._on_rf_match(slot, signal)

    @callback
    def _on_rf_match(self, slot: str, signal: CapturedSignal) -> None:
        """React to an externally received code.

        Default is a no-op (e.g. buttons). Platforms with state override this
        to update their optimistic state from the matched slot.
        """

    @callback
    def _handle_coordinator_update(self) -> None:
        """Self-remove when deleted; re-sync matcher and refresh otherwise.

        Re-registering on every coordinator update keeps the matcher in sync
        after a command is (re-)learned, edited or removed.
        """
        if self.code is None:
            if not self._removing:
                self._removing = True
                self.hass.async_create_task(self.async_remove(force_remove=True))
            return
        self._async_unregister_matcher()
        self._async_register_matcher()
        super()._handle_coordinator_update()

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _command(self, slot: str) -> RFCommandData | None:
        """Return the command stored in a slot, if any."""
        code = self.code
        return code.commands.get(slot) if code else None

    async def _async_send_slot(self, slot: str, repeat: int | None = None) -> None:
        """Transmit the command of a slot through the device transmitter.

        Raises:
            HomeAssistantError: If the slot is not configured or sending
                fails.
        """
        command = self._command(slot)
        if command is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="slot_not_configured",
                translation_placeholders={
                    "slot": slot,
                    "name": self.code.name if self.code else self._code_id,
                },
            )
        # Ignore our own transmission if the receiver picks it up.
        self._suppress_receive_until = self.hass.loop.time() + SELF_ECHO_SUPPRESS_S
        rf_manager = get_domain_data(self.hass).rf_manager
        await rf_manager.async_send(
            command, self.rf_device.transmitter, repeat=repeat
        )


class RFReceiveCodeEntity(RFCodeEntity):
    """Base for receive-only entities (sensor, binary_sensor).

    Receiving is handled by :class:`RFCodeEntity` via the shared
    ``CodeMatcher``; these platforms simply override ``_on_rf_match`` to
    turn a matched slot into their state. Kept as a distinct class to make
    the receive-only intent explicit at the platform level.
    """


@callback
def async_setup_code_platform(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
    entity_type: str,
    factory: CodeEntityFactory,
) -> None:
    """Create entities for all codes of a type and add new ones live.

    A coordinator listener diffs the stored codes on every update, so codes
    created through the options flow appear immediately without reloading
    the config entry. Deleted codes remove their entity via
    ``RFCodeEntity._handle_coordinator_update``.
    """
    coordinator = entry.runtime_data
    known_ids: set[str] = set()

    @callback
    def _async_sync_entities() -> None:
        codes: Iterable[RFCode] = coordinator.data.codes.values()
        new_codes = [
            code
            for code in codes
            if code.entity_type == entity_type and code.id not in known_ids
        ]
        if not new_codes:
            return
        known_ids.update(code.id for code in new_codes)
        async_add_entities(factory(coordinator, code) for code in new_codes)

    entry.async_on_unload(coordinator.async_add_listener(_async_sync_entities))
    _async_sync_entities()
