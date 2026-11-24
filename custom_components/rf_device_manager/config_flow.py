"""Config and options flows for RF Device Manager.

The config flow creates one entry per managed RF device (create or
duplicate); the options flow provides the full management UI:

* device settings (name, transmitter, frequency, repeats, enable/disable)
* the "add RF code" wizard: entity type, type settings, then one learn or
  manual-entry pass per command slot with progress display, multi-signal
  selection, live testing and a final confirmation
* code management: edit, re-learn single commands, test, enable/disable,
  delete
* bulk button creation: paste a list of button names and learn them back to
  back - one stateless button entity per name
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    ConstantSelector,
    ConstantSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    IconSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    ObjectSelector,
    ObjectSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from . import rf_analysis
from .const import (
    CONF_BRIGHTNESS_STEPS,
    CONF_BUTTON_NAMES,
    CONF_DEFAULT_REPEAT,
    CONF_DESCRIPTION,
    CONF_DEVICE_CLASS,
    CONF_DEVICE_ID,
    CONF_DUPLICATE_FROM,
    CONF_ENABLED,
    CONF_ENTITY_TYPE,
    CONF_FREQUENCY,
    CONF_FREQUENCY_CUSTOM,
    CONF_FREQUENCY_PRESET,
    CONF_HAS_BRIGHTNESS,
    CONF_HAS_DIRECTION,
    CONF_HAS_OFF,
    CONF_HAS_OSCILLATE,
    CONF_HAS_STOP,
    CONF_ICON,
    CONF_INPUT_FORMAT,
    CONF_NAME,
    CONF_NUMBER_MODE,
    CONF_OFF_DELAY,
    CONF_OPTIONS,
    CONF_PULSE_LENGTH,
    CONF_RC_PROTOCOL,
    CONF_ROTATION_RESET,
    CONF_REPEAT,
    CONF_SOURCE_ENTRY,
    CONF_SPEED_COUNT,
    CONF_TRANSMITTER,
    CONF_UNIT,
    CONF_VALUES,
    DEFAULT_FREQUENCY,
    DEFAULT_REPEAT,
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
    ENTITY_TYPES,
    FREQUENCY_PRESET_CUSTOM,
    FREQUENCY_PRESETS,
    PROTOCOL_B0,
    PROTOCOL_BINARY,
    PROTOCOL_PRONTO,
    PROTOCOL_RAW,
    PROTOCOL_TRISTATE,
    RADIO_FREQUENCY_DOMAIN,
)
from .device_manager import async_get_or_create_domain_data, get_domain_data
from .models import (
    CapturedSignal,
    RFCode,
    RFCommandData,
    RFDeviceConfig,
    build_slots,
    parse_csv,
    slot_label,
)
from .rf_manager import async_list_transmitters, async_validate_transmitter

_LOGGER = logging.getLogger(__name__)

_SWITCH_DEVICE_CLASSES = ["switch", "outlet"]
_COVER_DEVICE_CLASSES = [
    "awning",
    "blind",
    "curtain",
    "damper",
    "door",
    "garage",
    "gate",
    "shade",
    "shutter",
    "window",
]
_BINARY_SENSOR_DEVICE_CLASSES = [
    "door",
    "garage_door",
    "gas",
    "moisture",
    "motion",
    "occupancy",
    "opening",
    "smoke",
    "sound",
    "vibration",
    "window",
]


def _frequency_preset_selector() -> SelectSelector:
    """Build the frequency preset selector (presets + custom)."""
    options: list[SelectOptionDict] = [
        SelectOptionDict(value=str(hz), label=label)
        for label, hz in FREQUENCY_PRESETS.items()
    ]
    options.append(
        SelectOptionDict(value=FREQUENCY_PRESET_CUSTOM, label="Custom frequency…")
    )
    return SelectSelector(
        SelectSelectorConfig(
            options=options,
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="frequency_preset",
        )
    )


# Conditional field visibility was added in Home Assistant Core 2026.11.
# Keep a two-step compatibility flow for older supported Core releases.
_HAS_CONDITIONAL_FORM_FIELDS = (
    hasattr(data_entry_flow, "Required")
    and hasattr(data_entry_flow, "Optional")
    and hasattr(data_entry_flow, "probatio")
)

_MANUAL_FORMATS = (
    PROTOCOL_RAW,
    PROTOCOL_B0,
    PROTOCOL_TRISTATE,
    PROTOCOL_BINARY,
    PROTOCOL_PRONTO,
)


def _manual_format_selector() -> SelectSelector:
    """Choose the manual RF input format from a compact dropdown."""
    return SelectSelector(
        SelectSelectorConfig(
            options=list(_MANUAL_FORMATS),
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="input_format",
        )
    )


def _manual_format_help_selector(input_format: str) -> ConstantSelector:
    """Build the explanation shown for the selected manual input format."""
    return ConstantSelector(
        ConstantSelectorConfig(
            value=input_format,
            translation_key=f"manual_format_{input_format}.options",
        )
    )


def _manual_variant_key(input_format: str) -> str:
    """Return the form field name for variants of one input format."""
    return f"variants_{input_format}"


def _manual_repeat_selector() -> NumberSelector:
    """Build the repeat-count selector shared by both manual form paths."""
    return NumberSelector(
        NumberSelectorConfig(min=0, max=20, step=1, mode=NumberSelectorMode.BOX)
    )


def _manual_pulse_length_selector() -> NumberSelector:
    """Build the optional pulse-length selector used by encoded formats."""
    return NumberSelector(
        NumberSelectorConfig(
            min=50,
            max=5000,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="µs",
        )
    )


def _manual_rc_protocol_selector() -> SelectSelector:
    """Build the rc-switch protocol selector used by encoded formats."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[str(protocol) for protocol in rf_analysis.RC_PROTOCOLS],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _command_analysis(command: RFCommandData) -> dict[str, Any]:
    """Build the common analysis view used by confirmation and code details."""
    return {
        **command.extra,
        "pulse_count": len(command.data),
        "frame_count": command.extra.get("frame_count", 1),
        "base_unit_us": command.pulse_length,
        "bit_count": command.extra.get("bit_count")
        or len(command.extra.get("bits", ""))
        or None,
        "tristate": command.extra.get("tristate")
        or (command.code if command.protocol == PROTOCOL_TRISTATE else None),
    }


def _manual_variant_selector(input_format: str) -> ObjectSelector:
    """Build one repeatable command-variant list for every manual format.

    Binary and tri-state values remain single-line; the other formats use
    multiline editors. A shared ObjectSelector keeps the section heading,
    add button and list layout consistent across all formats.
    """
    multiline = input_format not in (PROTOCOL_BINARY, PROTOCOL_TRISTATE)
    return ObjectSelector(
        ObjectSelectorConfig(
            fields={
                "value": {
                    "selector": TextSelector(
                        TextSelectorConfig(multiline=multiline)
                    ),
                    "required": True,
                }
            },
            multiple=True,
            label_field="value",
            translation_key="manual_command_variants",
        )
    )


def _manual_dynamic_schema() -> Any:
    """Build the single-page manual form for Core versions with field visibility."""
    fields: dict[Any, Any] = {}

    # Show the format selector first, followed by its matching explanation.
    fields[
        data_entry_flow.Required(CONF_INPUT_FORMAT, default=PROTOCOL_RAW)
    ] = _manual_format_selector()

    for input_format in _MANUAL_FORMATS:
        visible = {"field": CONF_INPUT_FORMAT, "value": input_format}
        fields[
            data_entry_flow.Optional(
                f"format_help_{input_format}", visible=visible
            )
        ] = _manual_format_help_selector(input_format)

    for input_format in _MANUAL_FORMATS:
        visible = {"field": CONF_INPUT_FORMAT, "value": input_format}
        fields[
            data_entry_flow.Required(
                _manual_variant_key(input_format), visible=visible
            )
        ] = _manual_variant_selector(input_format)

    encoded_formats = {
        "field": CONF_INPUT_FORMAT,
        "operator": "in",
        "value": [PROTOCOL_BINARY, PROTOCOL_TRISTATE],
    }
    fields[
        data_entry_flow.Optional(CONF_PULSE_LENGTH, visible=encoded_formats)
    ] = _manual_pulse_length_selector()
    fields[
        data_entry_flow.Required(
            CONF_RC_PROTOCOL, default="1", visible=encoded_formats
        )
    ] = _manual_rc_protocol_selector()
    fields[data_entry_flow.Optional(CONF_REPEAT)] = _manual_repeat_selector()
    return data_entry_flow.probatio.Schema(fields)


def _device_schema() -> vol.Schema:
    """Schema shared by create, duplicate, reconfigure and device settings."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME): TextSelector(),
            vol.Optional(CONF_DESCRIPTION, default=""): TextSelector(),
            vol.Required(CONF_TRANSMITTER): EntitySelector(
                EntitySelectorConfig(domain=RADIO_FREQUENCY_DOMAIN)
            ),
            vol.Required(
                CONF_FREQUENCY_PRESET, default=str(DEFAULT_FREQUENCY)
            ): _frequency_preset_selector(),
            vol.Optional(CONF_FREQUENCY_CUSTOM): NumberSelector(
                NumberSelectorConfig(
                    min=100_000_000,
                    max=1_000_000_000,
                    step=1_000,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="Hz",
                )
            ),
            vol.Required(CONF_DEFAULT_REPEAT, default=DEFAULT_REPEAT): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=20, step=1, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Optional(CONF_ROTATION_RESET, default=False): BooleanSelector(),
        }
    )


def _validate_device_input(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate the shared device form and normalize it into options.

    Returns ``(options, errors)``; ``options`` is only meaningful when
    ``errors`` is empty.
    """
    errors: dict[str, str] = {}

    preset = user_input.get(CONF_FREQUENCY_PRESET, str(DEFAULT_FREQUENCY))
    if preset == FREQUENCY_PRESET_CUSTOM:
        custom = user_input.get(CONF_FREQUENCY_CUSTOM)
        if not custom:
            errors[CONF_FREQUENCY_CUSTOM] = "frequency_required"
            frequency = DEFAULT_FREQUENCY
        else:
            frequency = int(custom)
    else:
        frequency = int(preset)

    transmitter = user_input.get(CONF_TRANSMITTER, "")
    if not errors:
        if not async_list_transmitters(hass):
            errors["base"] = "no_transmitters"
        elif error := async_validate_transmitter(hass, transmitter, frequency):
            if error == "transmitter_not_found":
                errors[CONF_TRANSMITTER] = error
            else:
                errors["base"] = error

    options = {
        CONF_NAME: user_input.get(CONF_NAME, "").strip(),
        CONF_DESCRIPTION: user_input.get(CONF_DESCRIPTION, "").strip(),
        CONF_TRANSMITTER: transmitter,
        CONF_FREQUENCY: frequency,
        CONF_DEFAULT_REPEAT: int(user_input.get(CONF_DEFAULT_REPEAT, DEFAULT_REPEAT)),
        CONF_ROTATION_RESET: bool(user_input.get(CONF_ROTATION_RESET, False)),
        CONF_ENABLED: user_input.get(CONF_ENABLED, True),
    }
    if not options[CONF_NAME]:
        errors[CONF_NAME] = "name_required"
    return options, errors


def _parse_button_names(raw: str) -> list[str]:
    """Split the bulk-add text into button names.

    One name per line; a single line is split on commas instead, so both
    "Off, Speed 1, Speed 2" and a pasted list work.
    """
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) == 1 and "," in lines[0]:
        return parse_csv(lines[0])
    return lines


def _suggested_from_options(options: dict[str, Any]) -> dict[str, Any]:
    """Convert stored options back into form suggested values."""
    frequency = int(options.get(CONF_FREQUENCY, DEFAULT_FREQUENCY))
    known = {str(hz) for hz in FREQUENCY_PRESETS.values()}
    if str(frequency) in known:
        preset, custom = str(frequency), None
    else:
        preset, custom = FREQUENCY_PRESET_CUSTOM, frequency
    suggested: dict[str, Any] = {
        CONF_NAME: options.get(CONF_NAME, ""),
        CONF_DESCRIPTION: options.get(CONF_DESCRIPTION, ""),
        CONF_TRANSMITTER: options.get(CONF_TRANSMITTER, ""),
        CONF_FREQUENCY_PRESET: preset,
        CONF_DEFAULT_REPEAT: options.get(CONF_DEFAULT_REPEAT, DEFAULT_REPEAT),
        CONF_ROTATION_RESET: bool(options.get(CONF_ROTATION_RESET, False)),
    }
    if custom is not None:
        suggested[CONF_FREQUENCY_CUSTOM] = custom
    return suggested


class RFDeviceManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create a new managed RF device (fresh or duplicated)."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize flow state."""
        self._duplicate_source: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RFDeviceManagerOptionsFlow:
        """Return the options flow handler."""
        return RFDeviceManagerOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point: choose between creating and duplicating a device."""
        if not self._async_current_entries():
            return await self.async_step_create_device()
        return self.async_show_menu(
            step_id="user", menu_options=["create_device", "duplicate_device"]
        )

    async def async_step_create_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the properties of a new RF device."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options, errors = _validate_device_input(self.hass, user_input)
            if not errors:
                return await self._async_create_device_entry(options)

        suggested: dict[str, Any] = {}
        if self._duplicate_source is not None:
            suggested = _suggested_from_options(dict(self._duplicate_source.options))
            suggested[CONF_NAME] = f"{self._duplicate_source.title} (copy)"
        elif user_input is not None:
            suggested = user_input

        transmitters = async_list_transmitters(self.hass)
        if not transmitters and not errors:
            errors["base"] = "no_transmitters"
        return self.async_show_form(
            step_id="create_device",
            data_schema=self.add_suggested_values_to_schema(
                _device_schema(), suggested
            ),
            errors=errors,
            description_placeholders={
                "transmitter_count": str(len(transmitters)),
            },
        )

    async def _async_create_device_entry(
        self, options: dict[str, Any]
    ) -> ConfigFlowResult:
        """Create the config entry (and store record) for a new device."""
        device = RFDeviceConfig.create(
            name=options[CONF_NAME],
            transmitter=options[CONF_TRANSMITTER],
            description=options[CONF_DESCRIPTION],
            frequency=options[CONF_FREQUENCY],
            default_repeat=options[CONF_DEFAULT_REPEAT],
        )
        await self.async_set_unique_id(device.id)
        self._abort_if_unique_id_configured()

        data: dict[str, Any] = {CONF_DEVICE_ID: device.id}
        if self._duplicate_source is not None:
            data[CONF_DUPLICATE_FROM] = self._duplicate_source.data[CONF_DEVICE_ID]
        return self.async_create_entry(
            title=device.name, data=data, options=options
        )

    async def async_step_duplicate_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the device to duplicate."""
        entries = self._async_current_entries(include_ignore=False)
        if not entries:
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            self._duplicate_source = self.hass.config_entries.async_get_entry(
                user_input[CONF_SOURCE_ENTRY]
            )
            if self._duplicate_source is None:
                return self.async_abort(reason="no_devices")
            return await self.async_step_create_device()

        options = [
            SelectOptionDict(value=entry.entry_id, label=entry.title)
            for entry in entries
        ]
        return self.async_show_form(
            step_id="duplicate_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SOURCE_ENTRY): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure the base settings of an existing device."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            options, errors = _validate_device_input(self.hass, user_input)
            if not errors:
                # The reconfigure form has no "enabled" field; keep the
                # current state instead of re-enabling a disabled device.
                options[CONF_ENABLED] = entry.options.get(CONF_ENABLED, True)
                return self.async_update_reload_and_abort(
                    entry, title=options[CONF_NAME], options=options
                )
        suggested = (
            user_input
            if user_input is not None
            else _suggested_from_options(dict(entry.options))
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _device_schema(), suggested
            ),
            errors=errors,
            description_placeholders={"name": entry.title},
        )


class RFDeviceManagerOptionsFlow(OptionsFlow):
    """Per-device management: settings, codes, learning."""

    def __init__(self) -> None:
        """Initialize flow state."""
        self._code: RFCode | None = None
        self._editing_existing = False
        self._slots: list = []
        self._slot_index = 0
        self._pending: RFCommandData | None = None
        self._signals: list[CapturedSignal] = []
        self._learn_task: asyncio.Task | None = None
        self._selected_code_id: str | None = None
        self._test_note = ""
        self._pending_variants: list[RFCommandData] = []
        self._manual_format: str | None = None
        # Bulk button creation state.
        self._bulk_queue: list[RFCode] = []
        self._bulk_total = 0
        self._bulk_saved: list[str] = []
        self._bulk_skipped: list[str] = []

    @property
    def _bulk_active(self) -> bool:
        """Return whether the bulk button wizard is running."""
        return self._bulk_total > 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _language(self) -> str:
        """Return the active UI language for slot labels."""
        return self.hass.config.language or "en"

    def _device(self) -> RFDeviceConfig:
        """Return the live device config from the store."""
        store = get_domain_data(self.hass).store
        device = store.async_get_device(self.config_entry.data[CONF_DEVICE_ID])
        if device is None:  # pragma: no cover - entry always syncs the store
            raise ValueError("Device missing from store")
        return device

    def _codes(self) -> dict[str, RFCode]:
        """Return the device's codes from the store."""
        store = get_domain_data(self.hass).store
        return store.async_codes_for_device(self.config_entry.data[CONF_DEVICE_ID])

    def _current_slot_key(self) -> str:
        """Return the key of the slot currently being configured."""
        return self._slots[self._slot_index].key

    def _slot_placeholders(self) -> dict[str, str]:
        """Placeholders describing the current slot and wizard progress."""
        assert self._code is not None
        hub = get_domain_data(self.hass).rf_manager.hub
        receivers = hub.async_receiver_names()
        if self._bulk_active:
            done = self._bulk_total - len(self._bulk_queue)
            return {
                "code_name": self._device().name,
                "slot": self._code.name,
                "position": f"{done}/{self._bulk_total}",
                "receivers": ", ".join(receivers) if receivers else "—",
            }
        return {
            "code_name": self._code.name,
            "slot": slot_label(self._current_slot_key(), self._language),
            "position": f"{self._slot_index + 1}/{len(self._slots)}",
            "receivers": ", ".join(receivers) if receivers else "—",
        }

    def _finish(self) -> ConfigFlowResult:
        """Close the flow without touching the entry options.

        Passing the unchanged options back means no reload is triggered;
        code changes propagate live through the coordinator.
        """
        return self.async_create_entry(data=dict(self.config_entry.options))

    @callback
    def _async_cancel_learn_task(self) -> None:
        """Cancel a pending learn task."""
        if self._learn_task is not None and not self._learn_task.done():
            self._learn_task.cancel()
        self._learn_task = None

    def async_remove(self) -> None:
        """Flow was aborted/closed: stop listening for RF signals."""
        self._async_cancel_learn_task()

    # ------------------------------------------------------------------
    # Main menu + device settings
    # ------------------------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the management menu."""
        await async_get_or_create_domain_data(self.hass)
        menu = ["device_settings", "add_buttons", "add_code"]
        if self._codes():
            menu.append("manage_codes")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_device_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the base device settings."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options, errors = _validate_device_input(self.hass, user_input)
            if not errors:
                options[CONF_ENABLED] = user_input.get(CONF_ENABLED, True)
                if options[CONF_NAME] != self.config_entry.title:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, title=options[CONF_NAME]
                    )
                return self.async_create_entry(data=options)

        schema = _device_schema().extend(
            {vol.Required(CONF_ENABLED, default=True): BooleanSelector()}
        )
        suggested = (
            user_input
            if user_input is not None
            else {
                **_suggested_from_options(dict(self.config_entry.options)),
                CONF_ENABLED: self.config_entry.options.get(CONF_ENABLED, True),
            }
        )
        return self.async_show_form(
            step_id="device_settings",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Add code wizard
    # ------------------------------------------------------------------

    async def async_step_add_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect name and entity type of a new RF code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input.get(CONF_NAME, "").strip()
            if not name:
                errors[CONF_NAME] = "name_required"
            else:
                device = self._device()
                settings: dict[str, Any] = {}
                if icon := user_input.get(CONF_ICON):
                    settings[CONF_ICON] = icon
                self._code = RFCode.create(
                    device_id=device.id,
                    name=name,
                    entity_type=user_input[CONF_ENTITY_TYPE],
                    description=user_input.get(CONF_DESCRIPTION, "").strip(),
                    settings=settings,
                )
                self._editing_existing = False
                repeat = user_input.get(CONF_REPEAT)
                self._code.settings[CONF_REPEAT] = (
                    int(repeat) if repeat is not None else device.default_repeat
                )
                return await self.async_step_type_settings()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(),
                vol.Optional(CONF_DESCRIPTION, default=""): TextSelector(),
                vol.Required(
                    CONF_ENTITY_TYPE, default=ENTITY_TYPE_SWITCH
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=ENTITY_TYPES,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="entity_type",
                    )
                ),
                vol.Optional(CONF_ICON): IconSelector(),
                vol.Optional(CONF_REPEAT): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=20, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="add_code", data_schema=schema, errors=errors
        )

    # ------------------------------------------------------------------
    # Bulk button wizard
    # ------------------------------------------------------------------

    async def async_step_add_buttons(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create several stateless button entities in one go.

        Each name becomes one ``button`` entity. The wizard then goes
        straight into learning for every button in turn: press the remote
        button, confirm, and the next one starts listening.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            names = _parse_button_names(user_input.get(CONF_BUTTON_NAMES, ""))
            existing = {code.name.casefold() for code in self._codes().values()}
            folded = [name.casefold() for name in names]
            if not names:
                errors[CONF_BUTTON_NAMES] = "invalid_button_names"
            elif len(set(folded)) != len(folded) or existing.intersection(folded):
                errors[CONF_BUTTON_NAMES] = "duplicate_button_names"
            else:
                device = self._device()
                repeat_raw = user_input.get(CONF_REPEAT)
                repeat = (
                    int(repeat_raw)
                    if repeat_raw is not None
                    else device.default_repeat
                )
                icon = user_input.get(CONF_ICON)
                self._bulk_queue = []
                for name in names:
                    settings: dict[str, Any] = {CONF_REPEAT: repeat}
                    if icon:
                        settings[CONF_ICON] = icon
                    self._bulk_queue.append(
                        RFCode.create(
                            device_id=device.id,
                            name=name,
                            entity_type=ENTITY_TYPE_BUTTON,
                            settings=settings,
                        )
                    )
                self._bulk_total = len(names)
                self._bulk_saved = []
                self._bulk_skipped = []
                return await self._async_next_bulk_button()

        schema = vol.Schema(
            {
                vol.Required(CONF_BUTTON_NAMES): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
                vol.Optional(CONF_ICON): IconSelector(),
                vol.Optional(CONF_REPEAT): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=20, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="add_buttons",
            data_schema=schema,
            errors=errors,
            description_placeholders={"device_name": self._device().name},
        )

    async def _async_next_bulk_button(self) -> ConfigFlowResult:
        """Start learning the next queued button, or show the summary."""
        if not self._bulk_queue:
            return await self.async_step_bulk_done()
        self._code = self._bulk_queue.pop(0)
        self._editing_existing = False
        self._slots = build_slots(ENTITY_TYPE_BUTTON, {})
        self._slot_index = 0
        self._pending = None
        self._pending_variants = []
        self._signals = []
        # Straight into listening - no "learn or enter manually?" menu per
        # button. A timeout still offers manual entry and skip.
        return await self.async_step_slot_learn()

    async def async_step_bulk_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Summarize the bulk run; entities already exist at this point."""
        if user_input is not None:
            self._bulk_total = 0
            self._bulk_queue = []
            return self._finish()
        return self.async_show_form(
            step_id="bulk_done",
            data_schema=vol.Schema({}),
            description_placeholders={
                "saved": ", ".join(self._bulk_saved) or "—",
                "skipped": ", ".join(self._bulk_skipped) or "—",
                "saved_count": str(len(self._bulk_saved)),
                "total": str(self._bulk_total),
            },
        )

    async def async_step_type_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect entity-type specific settings, then start the slot loop."""
        assert self._code is not None
        entity_type = self._code.entity_type
        schema_fields = self._type_settings_fields(entity_type)
        if not schema_fields:
            return await self._async_start_slot_loop()

        errors: dict[str, str] = {}
        if user_input is not None:
            settings, errors = self._parse_type_settings(entity_type, user_input)
            if not errors:
                self._code.settings.update(settings)
                return await self._async_start_slot_loop()

        schema = vol.Schema(schema_fields)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="type_settings",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "code_name": self._code.name,
                "entity_type": entity_type,
            },
        )

    @staticmethod
    def _type_settings_fields(entity_type: str) -> dict[Any, Any]:
        """Return the voluptuous fields of the type settings form."""
        if entity_type == ENTITY_TYPE_SWITCH:
            return {
                vol.Required(CONF_HAS_OFF, default=True): BooleanSelector(),
                vol.Optional(CONF_DEVICE_CLASS): SelectSelector(
                    SelectSelectorConfig(
                        options=_SWITCH_DEVICE_CLASSES,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="switch_device_class",
                    )
                ),
            }
        if entity_type == ENTITY_TYPE_LIGHT:
            return {
                vol.Required(CONF_HAS_OFF, default=True): BooleanSelector(),
                vol.Required(CONF_HAS_BRIGHTNESS, default=False): BooleanSelector(),
                vol.Required(CONF_BRIGHTNESS_STEPS, default=10): NumberSelector(
                    NumberSelectorConfig(
                        min=2, max=50, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        if entity_type == ENTITY_TYPE_FAN:
            return {
                vol.Required(CONF_SPEED_COUNT, default=3): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=9, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(CONF_HAS_OSCILLATE, default=False): BooleanSelector(),
                vol.Required(CONF_HAS_DIRECTION, default=False): BooleanSelector(),
            }
        if entity_type == ENTITY_TYPE_COVER:
            return {
                vol.Required(CONF_HAS_STOP, default=True): BooleanSelector(),
                vol.Optional(CONF_DEVICE_CLASS): SelectSelector(
                    SelectSelectorConfig(
                        options=_COVER_DEVICE_CLASSES,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="cover_device_class",
                    )
                ),
            }
        if entity_type == ENTITY_TYPE_SELECT:
            return {vol.Required(CONF_OPTIONS): TextSelector()}
        if entity_type == ENTITY_TYPE_NUMBER:
            return {
                vol.Required(CONF_VALUES): TextSelector(),
                vol.Optional(CONF_UNIT): TextSelector(),
                vol.Required(CONF_NUMBER_MODE, default="slider"): SelectSelector(
                    SelectSelectorConfig(
                        options=["slider", "box"],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="number_mode",
                    )
                ),
            }
        if entity_type == ENTITY_TYPE_SENSOR:
            return {
                vol.Required(CONF_VALUES): TextSelector(),
                vol.Optional(CONF_UNIT): TextSelector(),
            }
        if entity_type == ENTITY_TYPE_BINARY_SENSOR:
            return {
                vol.Required(CONF_HAS_OFF, default=False): BooleanSelector(),
                vol.Required(CONF_OFF_DELAY, default=5): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=3600, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(CONF_DEVICE_CLASS): SelectSelector(
                    SelectSelectorConfig(
                        options=_BINARY_SENSOR_DEVICE_CLASSES,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="binary_sensor_device_class",
                    )
                ),
            }
        return {}  # button and unknown types have no extra settings

    @staticmethod
    def _parse_type_settings(
        entity_type: str, user_input: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Normalize and validate type settings; returns (settings, errors)."""
        errors: dict[str, str] = {}
        settings = dict(user_input)

        if entity_type == ENTITY_TYPE_SELECT:
            options = parse_csv(user_input.get(CONF_OPTIONS, ""))
            if not options or len(set(options)) != len(options):
                errors[CONF_OPTIONS] = "invalid_options"
            settings[CONF_OPTIONS] = options
        elif entity_type in (ENTITY_TYPE_NUMBER, ENTITY_TYPE_SENSOR):
            raw_values = parse_csv(user_input.get(CONF_VALUES, ""))
            if not raw_values:
                errors[CONF_VALUES] = "invalid_values"
            elif entity_type == ENTITY_TYPE_NUMBER:
                try:
                    settings[CONF_VALUES] = [float(value) for value in raw_values]
                except ValueError:
                    errors[CONF_VALUES] = "invalid_values"
            else:
                settings[CONF_VALUES] = raw_values
        for key in (CONF_SPEED_COUNT, CONF_BRIGHTNESS_STEPS, CONF_OFF_DELAY):
            if key in settings and settings[key] is not None:
                settings[key] = int(settings[key])
        return settings, errors

    async def _async_start_slot_loop(self) -> ConfigFlowResult:
        """Build the slot list and enter the per-slot wizard."""
        assert self._code is not None
        self._slots = build_slots(self._code.entity_type, self._code.settings)
        self._slot_index = 0
        self._pending = None
        return await self.async_step_slot_menu()

    # ------------------------------------------------------------------
    # Slot loop
    # ------------------------------------------------------------------

    async def async_step_slot_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how to provide the command for the current slot."""
        assert self._code is not None
        self._pending = None
        self._pending_variants = []
        self._signals = []
        self._manual_format = None
        # Refresh the receiver list so the menu (and the learn availability
        # check) reflect the current state of the ESPHome nodes.
        await get_domain_data(self.hass).rf_manager.hub.async_discover()
        spec = self._slots[self._slot_index]
        menu = ["slot_learn", "slot_manual"]
        if not spec.required or spec.key in self._code.commands or self._bulk_active:
            menu.append("slot_skip")
        return self.async_show_menu(
            step_id="slot_menu",
            menu_options=menu,
            description_placeholders=self._slot_placeholders(),
        )

    async def async_step_slot_learn(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Listen for RF signals with a progress indicator."""
        if self._learn_task is None:
            rf_manager = get_domain_data(self.hass).rf_manager
            # Learning needs a receiver; without one the wait is pointless.
            await rf_manager.hub.async_discover()
            if not rf_manager.hub.async_receiver_names():
                return await self.async_step_slot_no_receiver()
            session = rf_manager.new_learn_session()
            self._signals = []
            self._learn_task = self.hass.async_create_task(session.async_capture())

        if not self._learn_task.done():
            return self.async_show_progress(
                step_id="slot_learn",
                progress_action="learn_waiting",
                progress_task=self._learn_task,
                description_placeholders=self._slot_placeholders(),
            )

        try:
            self._signals = self._learn_task.result()
        except TimeoutError:
            self._signals = []
        except asyncio.CancelledError:
            self._signals = []
        except Exception:  # noqa: BLE001 - surface as a failed learn attempt
            _LOGGER.exception("Learning failed unexpectedly")
            self._signals = []
        finally:
            self._learn_task = None

        if not self._signals:
            return self.async_show_progress_done(next_step_id="slot_learn_failed")
        return self.async_show_progress_done(next_step_id="slot_learn_done")

    async def async_step_slot_learn_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Nothing was received: offer retry, manual entry or skip."""
        assert self._code is not None
        spec = self._slots[self._slot_index]
        menu = ["slot_learn", "slot_manual"]
        if not spec.required or self._bulk_active:
            menu.append("slot_skip")
        return self.async_show_menu(
            step_id="slot_learn_failed",
            menu_options=menu,
            description_placeholders=self._slot_placeholders(),
        )

    async def async_step_slot_no_receiver(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """No RF receiver is available: warn and offer alternatives.

        Learning cannot work without a receiver, so instead of waiting for a
        signal that can never arrive, the user is warned and can retry (after
        fixing the ESPHome node), enter the code manually, or skip.
        """
        assert self._code is not None
        spec = self._slots[self._slot_index]
        menu = ["slot_learn", "slot_manual"]
        if not spec.required or spec.key in self._code.commands or self._bulk_active:
            menu.append("slot_skip")
        return self.async_show_menu(
            step_id="slot_no_receiver",
            menu_options=menu,
            description_placeholders=self._slot_placeholders(),
        )

    async def async_step_slot_learn_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user select one or more captured signals as command variants."""
        errors: dict[str, str] = {}

        # A single distinct capture needs no selection page.
        if len(self._signals) == 1 and user_input is None:
            self._pending_variants = [self._async_build_pending(self._signals[0])]
            self._pending = self._pending_variants[0]
            return await self.async_step_slot_confirm()

        if user_input is not None:
            selected = user_input.get("signals", [])
            if not selected:
                errors["base"] = "select_signal_required"
            else:
                # Preserve the capture list order, not the order in which the
                # user happened to tick the checkboxes. This is important for
                # remotes whose codes follow a fixed rotation.
                indices = sorted({int(index) for index in selected})
                if any(index < 0 or index >= len(self._signals) for index in indices):
                    errors["base"] = "select_signal_required"
                else:
                    self._pending_variants = [
                        self._async_build_pending(self._signals[index])
                        for index in indices
                    ]
                    self._pending = self._pending_variants[0]
                    return await self.async_step_slot_confirm()

        options = [
            SelectOptionDict(
                value=str(index),
                label=(
                    f"Signal {index + 1} — "
                    f"{rf_analysis.describe_signal(signal.analysis, self._language)}"
                    f" (×{signal.analysis.get('times_seen', 1)})"
                ),
            )
            for index, signal in enumerate(self._signals)
        ]
        summary = "\n".join(
            f"- **Signal {index + 1}** · {signal.source} · "
            f"{rf_analysis.describe_signal(signal.analysis, self._language)}"
            for index, signal in enumerate(self._signals)
        )
        return self.async_show_form(
            step_id="slot_learn_done",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "signals",
                        default=[
                            str(index)
                            for index in rf_analysis.preselected_signal_indices(
                                [signal.analysis for signal in self._signals]
                            )
                        ],
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.LIST,
                            multiple=True,
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                **self._slot_placeholders(),
                "signals": summary,
            },
        )

    @callback
    def _async_build_pending(self, signal: CapturedSignal) -> RFCommandData:
        """Turn a captured signal into a command variant for the current slot."""
        assert self._code is not None
        device = self._device()
        analysis = signal.analysis
        extra = {
            key: analysis[key]
            for key in (*rf_analysis.STORED_DECODE_KEYS, "times_seen")
            if key in analysis
        }
        return RFCommandData(
            protocol=PROTOCOL_RAW,
            frequency=signal.frequency or device.frequency,
            data=list(signal.timings),
            pulse_length=analysis.get("base_unit_us"),
            repeat=int(self._code.settings.get(CONF_REPEAT, device.default_repeat)),
            source=f"learned:{signal.source}",
            fingerprint=analysis.get("fingerprint"),
            extra=extra,
        )

    async def async_step_slot_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter manual RF data using a format-specific form."""
        assert self._code is not None
        errors: dict[str, str] = {}

        if _HAS_CONDITIONAL_FORM_FIELDS:
            if user_input is not None:
                self._manual_format = user_input.get(CONF_INPUT_FORMAT)
                if self._manual_format not in _MANUAL_FORMATS:
                    errors["base"] = "invalid_input_format"
                else:
                    try:
                        self._pending_variants = self._parse_manual_input(user_input)
                    except rf_analysis.RFInputError as err:
                        errors["base"] = err.error_key
                    else:
                        self._pending = self._pending_variants[0]
                        return await self.async_step_slot_confirm()

            schema = _manual_dynamic_schema()
            if user_input is not None:
                schema = self.add_suggested_values_to_schema(schema, user_input)
            return self.async_show_form(
                step_id="slot_manual",
                data_schema=schema,
                errors=errors,
                description_placeholders=self._slot_placeholders(),
            )

        # Core versions before 2026.11 do not support conditional fields.
        # Keep the same dropdown, then show only the selected format's inputs.
        if user_input is not None:
            self._manual_format = user_input.get(CONF_INPUT_FORMAT)
            if self._manual_format not in _MANUAL_FORMATS:
                errors["base"] = "invalid_input_format"
            else:
                return await self.async_step_slot_manual_data()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_INPUT_FORMAT, default=PROTOCOL_RAW
                ): _manual_format_selector(),
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="slot_manual",
            data_schema=schema,
            errors=errors,
            description_placeholders=self._slot_placeholders(),
        )

    async def async_step_slot_manual_data(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Compatibility step for Core versions without conditional fields."""
        assert self._code is not None
        assert self._manual_format is not None

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._pending_variants = self._parse_manual_input(user_input)
            except rf_analysis.RFInputError as err:
                errors["base"] = err.error_key
            else:
                self._pending = self._pending_variants[0]
                return await self.async_step_slot_confirm()

        fields: dict[Any, Any] = {
            vol.Optional(f"format_help_{self._manual_format}"): (
                _manual_format_help_selector(self._manual_format)
            ),
            vol.Required(_manual_variant_key(self._manual_format)): (
                _manual_variant_selector(self._manual_format)
            ),
        }
        if self._manual_format in (PROTOCOL_BINARY, PROTOCOL_TRISTATE):
            fields.update(
                {
                    vol.Optional(CONF_PULSE_LENGTH): _manual_pulse_length_selector(),
                    vol.Required(CONF_RC_PROTOCOL, default="1"): (
                        _manual_rc_protocol_selector()
                    ),
                }
            )
        fields[vol.Optional(CONF_REPEAT)] = _manual_repeat_selector()

        schema = vol.Schema(fields)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="slot_manual_data",
            data_schema=schema,
            errors=errors,
            description_placeholders=self._slot_placeholders(),
        )

    def _parse_manual_input(
        self, user_input: dict[str, Any]
    ) -> list[RFCommandData]:
        """Parse all manually entered variants into transmittable commands."""
        assert self._code is not None
        assert self._manual_format is not None
        device = self._device()
        input_format = self._manual_format

        rows = user_input.get(_manual_variant_key(input_format))
        if not isinstance(rows, list) or not rows:
            raise rf_analysis.RFInputError(
                "manual_variant_required", "Enter at least one variant"
            )

        values: list[str] = []
        for index, row in enumerate(rows, start=1):
            if isinstance(row, str):
                value = row
            elif isinstance(row, dict):
                value = row.get("value")
            else:
                value = None
            if not isinstance(value, str) or not value.strip():
                raise rf_analysis.RFInputError(
                    "manual_variant_required",
                    f"Variant {index} is empty",
                )
            values.append(value)

        pulse_raw = user_input.get(CONF_PULSE_LENGTH)
        pulse_length = int(pulse_raw) if pulse_raw else None
        rc_protocol = int(user_input.get(CONF_RC_PROTOCOL, "1"))
        parsed_variants = rf_analysis.parse_manual_variants(
            input_format,
            values,
            pulse_length=pulse_length,
            protocol=rc_protocol,
        )

        repeat_raw = user_input.get(CONF_REPEAT)
        repeat = (
            int(repeat_raw)
            if repeat_raw is not None
            else int(self._code.settings.get(CONF_REPEAT, device.default_repeat))
        )

        commands: list[RFCommandData] = []
        for timings, code_text in parsed_variants:
            timings = rf_analysis.normalize(timings)
            analysis = rf_analysis.analyze(timings)
            extra = {
                key: analysis[key]
                for key in rf_analysis.STORED_DECODE_KEYS
                if key in analysis
            }
            commands.append(
                RFCommandData(
                    protocol=input_format,
                    frequency=device.frequency,
                    data=timings,
                    pulse_length=pulse_length or analysis.get("base_unit_us"),
                    code=code_text,
                    repeat=repeat,
                    source="manual",
                    fingerprint=analysis.get("fingerprint"),
                    extra=extra,
                )
            )
        return commands

    async def async_step_slot_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Preview the first selected variant; accept, test or retry the selection."""
        assert self._pending is not None
        pending = self._pending
        if not self._pending_variants:
            self._pending_variants = [pending]
        is_german = self._language.lower().startswith("de")
        variant_label = "Kommandovariante" if is_german else "Command variant"
        source_label = "Quelle" if is_german else "Source"
        timings_label = "Timing" if is_german else "Timings"
        variant_details: list[str] = []
        for index, command in enumerate(self._pending_variants, start=1):
            analysis = _command_analysis(command)
            variant_details.append(
                f"**{variant_label} {index}**\n\n"
                f"{rf_analysis.signal_details(analysis, self._language)}\n"
                f"{source_label}: {command.source}\n"
                f"{timings_label}: `{rf_analysis.preview_timings(command.data)}`"
            )
        notes = [
            note
            for note in (self._duplicate_warning(self._pending_variants), self._test_note)
            if note
        ]
        placeholders = {
            **self._slot_placeholders(),
            "variant_details": "\n\n".join(variant_details),
            "variant_count": str(len(self._pending_variants)),
            "test_note": "\n\n".join(notes),
        }
        self._test_note = ""
        return self.async_show_menu(
            step_id="slot_confirm",
            menu_options=["slot_accept", "slot_test", "slot_retry"],
            description_placeholders=placeholders,
        )

    def _duplicate_warning(self, pending_variants: list[RFCommandData]) -> str:
        """Warn when a pending command variant equals one already stored.

        Catches the classic bulk-learning slip of pressing the same remote
        button twice (or the previous one again). Every selected variant is
        compared against every stored variant of every other slot/code.
        """
        assert self._code is not None
        current_slot = self._current_slot_key()
        clashes: list[str] = []
        codes = dict(self._codes())
        codes[self._code.id] = self._code
        for pending in pending_variants:
            bits = pending.extra.get("bits")
            if not pending.fingerprint and not bits:
                continue
            for code in codes.values():
                for slot, variants in code.commands.items():
                    if code.id == self._code.id and slot == current_slot:
                        continue  # the command being replaced
                    same = any(
                        (
                            pending.fingerprint is not None
                            and command.fingerprint == pending.fingerprint
                        )
                        or (bits is not None and command.extra.get("bits") == bits)
                        for command in variants
                    )
                    if not same:
                        continue
                    label = (
                        code.name
                        if len(code.commands) == 1
                        else f"{code.name} / {slot_label(slot, self._language)}"
                    )
                    if label not in clashes:
                        clashes.append(label)
        if not clashes:
            return ""
        names = ", ".join(f"“{name}”" for name in clashes)
        if self._language.lower().startswith("de"):
            return f"⚠️ Gleicher Code wie {names} – falsche Taste gedrückt?"
        return f"⚠️ Same code as {names} – wrong button pressed?"

    async def async_step_slot_test(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send the first selected variant once for a live test."""
        assert self._pending is not None
        assert self._code is not None
        device = self._device()
        rf_manager = get_domain_data(self.hass).rf_manager
        variant_count = len(self._pending_variants) or 1
        log_context = (
            f"RF code {self._code.name!r}, slot {self._current_slot_key()}, "
            f"variant 1/{variant_count}, "
            f"fingerprint {self._pending.fingerprint or 'n/a'} (learn test)"
        )
        try:
            await rf_manager.async_send(
                self._pending, device.transmitter, log_context=log_context
            )
        except Exception as err:  # noqa: BLE001 - shown inline, flow continues
            self._test_note = f"❌ {err}"
        else:
            self._test_note = "✅"
        return await self.async_step_slot_confirm()

    async def async_step_slot_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discard the selected variants.

        With several captured signals, return to the multi-select list so the
        selection can be changed; otherwise re-enter the slot menu.
        """
        self._pending = None
        self._pending_variants = []
        if len(self._signals) > 1:
            return await self.async_step_slot_learn_done()
        return await self.async_step_slot_menu()

    async def async_step_slot_skip(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Skip an optional slot (keeps an existing command, if any)."""
        return await self._async_advance_slot()

    async def async_step_slot_accept(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the selected command variants into the slot and advance."""
        assert self._code is not None and self._pending is not None
        slot = self._current_slot_key()
        self._code.commands[slot] = list(self._pending_variants or [self._pending])
        get_domain_data(self.hass).rf_manager.reset_command_rotation(
            self._code.id, slot
        )
        self._pending = None
        self._pending_variants = []
        return await self._async_advance_slot()

    async def _async_advance_slot(self) -> ConfigFlowResult:
        """Move to the next slot or finish the wizard."""
        self._slot_index += 1
        if self._slot_index < len(self._slots):
            return await self.async_step_slot_menu()
        if self._editing_existing:
            assert self._code is not None
            get_domain_data(self.hass).device_manager.async_update_code(self._code)
            return self._finish()
        if self._bulk_active:
            assert self._code is not None
            # Save each button as soon as it is learned, so closing the
            # dialog half way keeps everything learned so far.
            if self._code.commands:
                get_domain_data(self.hass).device_manager.async_add_code(self._code)
                self._bulk_saved.append(self._code.name)
            else:
                self._bulk_skipped.append(self._code.name)
            return await self._async_next_bulk_button()
        return await self.async_step_save_code()

    async def async_step_save_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Final confirmation before the code is stored."""
        assert self._code is not None
        if user_input is not None:
            get_domain_data(self.hass).device_manager.async_add_code(self._code)
            return self._finish()

        missing_required = [
            spec.key
            for spec in self._slots
            if spec.required and spec.key not in self._code.commands
        ]
        return self.async_show_form(
            step_id="save_code",
            data_schema=vol.Schema({}),
            description_placeholders={
                "code_name": self._code.name,
                "entity_type": self._code.entity_type,
                "slots": ", ".join(
                    slot_label(slot, self._language) for slot in self._code.commands
                )
                or "—",
                "missing": ", ".join(
                    slot_label(slot, self._language) for slot in missing_required
                )
                or "—",
            },
        )

    # ------------------------------------------------------------------
    # Manage codes
    # ------------------------------------------------------------------

    async def async_step_manage_codes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a code to manage."""
        codes = self._codes()
        if not codes:
            return self.async_abort(reason="no_codes")
        if user_input is not None:
            self._selected_code_id = user_input["code"]
            return await self.async_step_code_menu()

        options = [
            SelectOptionDict(
                value=code.id,
                label=f"{code.name} ({code.entity_type})"
                + ("" if code.enabled else " ⏸"),
            )
            for code in sorted(codes.values(), key=lambda code: code.name.casefold())
        ]
        return self.async_show_form(
            step_id="manage_codes",
            data_schema=vol.Schema(
                {
                    vol.Required("code"): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
        )

    def _selected_code(self) -> RFCode | None:
        """Return the currently selected code from the store."""
        if self._selected_code_id is None:
            return None
        return get_domain_data(self.hass).store.async_get_code(self._selected_code_id)

    async def async_step_code_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the actions available for the selected code."""
        code = self._selected_code()
        if code is None:
            return self.async_abort(reason="no_codes")
        note, self._test_note = self._test_note, ""
        variant_label = (
            "Kommandovarianten"
            if self._language.lower().startswith("de")
            else "command variants"
        )
        return self.async_show_menu(
            step_id="code_menu",
            menu_options=[
                "code_view",
                "code_edit",
                "code_relearn",
                "code_test",
                "code_toggle",
                "code_delete",
            ],
            description_placeholders={
                "code_name": code.name,
                "entity_type": code.entity_type,
                "slots": ", ".join(
                    (
                        f"{slot_label(slot, self._language)} ({len(variants)} "
                        f"{variant_label})"
                        if len(variants) > 1 else slot_label(slot, self._language)
                    )
                    for slot, variants in code.commands.items()
                )
                or "—",
                "enabled": "✅" if code.enabled else "⏸",
                "test_note": note,
            },
        )

    async def async_step_code_view(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a read-only detail view of every command of the code.

        From here the user returns to the code menu to rework a command
        (re-learn), rename or delete it.
        """
        code = self._selected_code()
        if code is None:
            return self.async_abort(reason="no_codes")
        if user_input is not None:
            return await self.async_step_code_menu()

        german = self._language.lower().startswith("de")
        if code.commands:
            blocks: list[str] = []
            for slot, variants in code.commands.items():
                heading = slot_label(slot, self._language)
                if len(variants) > 1:
                    count_label = (
                        "Kommandovarianten"
                        if german
                        else "command variants"
                    )
                    heading += f" ({len(variants)} {count_label})"
                lines = [f"### {heading}"]
                for index, command in enumerate(variants, start=1):
                    if len(variants) > 1:
                        lines.append(f"**{index}.**")
                    lines.append(
                        rf_analysis.signal_details(
                            _command_analysis(command), self._language
                        )
                    )
                    lines.append(
                        f"**Timings:** `{rf_analysis.preview_timings(command.data)}`"
                    )
                blocks.append("\n\n".join(lines))
            body = "\n\n".join(blocks)
        else:
            body = "*Noch keine Befehle.*" if german else "*No commands yet.*"

        return self.async_show_form(
            step_id="code_view",
            data_schema=vol.Schema({}),
            description_placeholders={
                "code_name": code.name,
                "entity_type": code.entity_type,
                "enabled": "✅" if code.enabled else "⏸",
                "description": code.description or ("—"),
                "commands": body,
            },
        )

    async def async_step_code_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Rename/describe/re-icon the selected code."""
        code = self._selected_code()
        if code is None:
            return self.async_abort(reason="no_codes")
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input.get(CONF_NAME, "").strip()
            if not name:
                errors[CONF_NAME] = "name_required"
            else:
                code.name = name
                code.description = user_input.get(CONF_DESCRIPTION, "").strip()
                if icon := user_input.get(CONF_ICON):
                    code.settings[CONF_ICON] = icon
                else:
                    code.settings.pop(CONF_ICON, None)
                repeat = user_input.get(CONF_REPEAT)
                if repeat is not None:
                    code.settings[CONF_REPEAT] = int(repeat)
                    for variants in code.commands.values():
                        for command in variants:
                            command.repeat = int(repeat)
                get_domain_data(self.hass).device_manager.async_update_code(code)
                return self._finish()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(),
                vol.Optional(CONF_DESCRIPTION, default=""): TextSelector(),
                vol.Optional(CONF_ICON): IconSelector(),
                vol.Optional(CONF_REPEAT): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=20, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )
        suggested = user_input or {
            CONF_NAME: code.name,
            CONF_DESCRIPTION: code.description,
            CONF_ICON: code.settings.get(CONF_ICON),
            CONF_REPEAT: code.settings.get(CONF_REPEAT),
        }
        return self.async_show_form(
            step_id="code_edit",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_code_relearn(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a single command slot of the code to (re-)learn."""
        code = self._selected_code()
        if code is None:
            return self.async_abort(reason="no_codes")
        all_slots = build_slots(code.entity_type, code.settings)
        if user_input is not None:
            slot_key = user_input["slot"]
            spec = next((spec for spec in all_slots if spec.key == slot_key), None)
            self._code = code
            self._editing_existing = True
            self._slots = [spec] if spec is not None else all_slots
            self._slot_index = 0
            return await self.async_step_slot_menu()

        options = [
            SelectOptionDict(
                value=spec.key,
                label=slot_label(spec.key, self._language)
                + (" ✅" if spec.key in code.commands else " ∅"),
            )
            for spec in all_slots
        ]
        return self.async_show_form(
            step_id="code_relearn",
            data_schema=vol.Schema(
                {
                    vol.Required("slot"): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
            description_placeholders={"code_name": code.name},
        )

    async def async_step_code_test(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send one of the code's commands for testing."""
        code = self._selected_code()
        if code is None:
            return self.async_abort(reason="no_codes")
        if not code.commands:
            self._test_note = "∅"
            return await self.async_step_code_menu()
        if user_input is not None:
            slot = user_input["slot"]
            device = self._device()
            rf_manager = get_domain_data(self.hass).rf_manager
            variants = code.commands.get(slot)
            try:
                if not variants:
                    raise ValueError(slot)
                await rf_manager.async_send_variants(
                    variants,
                    device.transmitter,
                    code_id=code.id,
                    slot=slot,
                    context=f"RF code {code.name!r} (code test)",
                )
            except Exception as err:  # noqa: BLE001 - shown inline
                self._test_note = f"❌ {err}"
            else:
                self._test_note = "✅"
            return await self.async_step_code_menu()

        options = [
            SelectOptionDict(value=slot, label=slot_label(slot, self._language))
            for slot in code.commands
        ]
        return self.async_show_form(
            step_id="code_test",
            data_schema=vol.Schema(
                {
                    vol.Required("slot"): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
            description_placeholders={"code_name": code.name},
        )

    async def async_step_code_toggle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enable/disable the selected code."""
        code = self._selected_code()
        if code is None:
            return self.async_abort(reason="no_codes")
        code.enabled = not code.enabled
        get_domain_data(self.hass).device_manager.async_update_code(code)
        return await self.async_step_code_menu()

    async def async_step_code_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm and delete the selected code."""
        code = self._selected_code()
        if code is None:
            return self.async_abort(reason="no_codes")
        if user_input is not None:
            get_domain_data(self.hass).device_manager.async_delete_code(code.id)
            self._selected_code_id = None
            return self._finish()
        return self.async_show_form(
            step_id="code_delete",
            data_schema=vol.Schema({}),
            description_placeholders={
                "code_name": code.name,
                "entity_type": code.entity_type,
            },
        )
