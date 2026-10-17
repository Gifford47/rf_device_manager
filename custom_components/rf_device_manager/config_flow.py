"""Config and options flows for RF Device Manager.

The config flow creates one entry per managed RF device (create or
duplicate); the options flow provides the full management UI:

* device settings (name, transmitter, frequency, repeats, enable/disable)
* the "add RF code" wizard: entity type, type settings, then one learn or
  manual-entry pass per command slot with progress display, multi-signal
  selection, live testing and a final confirmation
* code management: edit, re-learn single commands, test, enable/disable,
  delete
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    IconSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from . import rf_analysis
from .const import (
    CONF_B0_CODE,
    CONF_BINARY_CODE,
    CONF_BRIGHTNESS_STEPS,
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
    CONF_PRONTO_CODE,
    CONF_PULSE_LENGTH,
    CONF_RAW_TIMINGS,
    CONF_RC_PROTOCOL,
    CONF_REPEAT,
    CONF_SOURCE_ENTRY,
    CONF_SPEED_COUNT,
    CONF_TRANSMITTER,
    CONF_TRISTATE_CODE,
    CONF_UNIT,
    CONF_VALUES,
    DEFAULT_FREQUENCY,
    DEFAULT_REPEAT,
    DOMAIN,
    ENTITY_TYPE_BINARY_SENSOR,
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
        CONF_ENABLED: user_input.get(CONF_ENABLED, True),
    }
    if not options[CONF_NAME]:
        errors[CONF_NAME] = "name_required"
    return options, errors


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
        domain_data = await async_get_or_create_domain_data(self.hass)
        # Warm the receiver cache so the learn wizard can show which
        # ESPHome RF receivers are available.
        await domain_data.rf_manager.hub.async_discover()
        menu = ["device_settings", "add_code"]
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
        # Refresh the receiver list so the menu (and the learn availability
        # check) reflect the current state of the ESPHome nodes.
        await get_domain_data(self.hass).rf_manager.hub.async_discover()
        spec = self._slots[self._slot_index]
        menu = ["slot_learn", "slot_manual"]
        if not spec.required or spec.key in self._code.commands:
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
        if not spec.required:
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
        if not spec.required or spec.key in self._code.commands:
            menu.append("slot_skip")
        return self.async_show_menu(
            step_id="slot_no_receiver",
            menu_options=menu,
            description_placeholders=self._slot_placeholders(),
        )

    async def async_step_slot_learn_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick one of the captured signals.

        A single capture skips straight to the confirmation step. Otherwise
        the user selects one from the list; the following confirmation step
        provides the "Test" and "Save" buttons (no toggle here).
        """
        if len(self._signals) == 1 and user_input is None:
            self._async_build_pending(self._signals[0])
            return await self.async_step_slot_confirm()

        if user_input is not None:
            self._async_build_pending(self._signals[int(user_input["signal"])])
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
                    vol.Required("signal", default="0"): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    ),
                }
            ),
            description_placeholders={
                **self._slot_placeholders(),
                "signals": summary,
            },
        )

    @callback
    def _async_build_pending(self, signal: CapturedSignal) -> None:
        """Turn a captured signal into the pending command of the slot."""
        assert self._code is not None
        device = self._device()
        analysis = signal.analysis
        extra = {
            key: analysis[key]
            for key in (
                "encoding",
                "bits",
                "bit_count",
                "hex",
                "tristate",
                "protocol_guess",
                "frame_count",
                "times_seen",
            )
            if key in analysis
        }
        self._pending = RFCommandData(
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
        """Manually enter RF data in one of the supported formats."""
        assert self._code is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._pending = self._parse_manual_input(user_input)
            except rf_analysis.RFInputError as err:
                errors["base"] = err.error_key
            else:
                return await self.async_step_slot_confirm()

        schema = vol.Schema(
            {
                vol.Required(CONF_INPUT_FORMAT, default=PROTOCOL_RAW): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            PROTOCOL_RAW,
                            PROTOCOL_B0,
                            PROTOCOL_TRISTATE,
                            PROTOCOL_BINARY,
                            PROTOCOL_PRONTO,
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="input_format",
                    )
                ),
                vol.Optional(CONF_RAW_TIMINGS): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
                vol.Optional(CONF_B0_CODE): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
                vol.Optional(CONF_TRISTATE_CODE): TextSelector(),
                vol.Optional(CONF_BINARY_CODE): TextSelector(),
                vol.Optional(CONF_PRONTO_CODE): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
                vol.Optional(CONF_PULSE_LENGTH): NumberSelector(
                    NumberSelectorConfig(
                        min=50, max=5000, step=1, mode=NumberSelectorMode.BOX,
                        unit_of_measurement="µs",
                    )
                ),
                vol.Required(CONF_RC_PROTOCOL, default="1"): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            str(protocol) for protocol in rf_analysis.RC_PROTOCOLS
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
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
            step_id="slot_manual",
            data_schema=schema,
            errors=errors,
            description_placeholders=self._slot_placeholders(),
        )

    def _parse_manual_input(self, user_input: dict[str, Any]) -> RFCommandData:
        """Build a command from manual input; raises ``RFInputError``."""
        assert self._code is not None
        device = self._device()
        input_format = user_input[CONF_INPUT_FORMAT]
        pulse_raw = user_input.get(CONF_PULSE_LENGTH)
        pulse_length = int(pulse_raw) if pulse_raw else None
        rc_protocol = int(user_input.get(CONF_RC_PROTOCOL, "1"))
        code_text: str | None = None
        frequency = device.frequency

        if input_format == PROTOCOL_RAW:
            timings = rf_analysis.parse_timings_text(
                user_input.get(CONF_RAW_TIMINGS) or ""
            )
        elif input_format == PROTOCOL_B0:
            code_text = (user_input.get(CONF_B0_CODE) or "").strip()
            timings = rf_analysis.parse_tasmota_rfraw(code_text)
        elif input_format == PROTOCOL_TRISTATE:
            code_text = (user_input.get(CONF_TRISTATE_CODE) or "").strip().upper()
            timings = rf_analysis.encode_tristate(
                code_text, pulse_length=pulse_length, protocol=rc_protocol
            )
        elif input_format == PROTOCOL_BINARY:
            code_text = (user_input.get(CONF_BINARY_CODE) or "").strip()
            timings = rf_analysis.encode_binary(
                code_text, pulse_length=pulse_length, protocol=rc_protocol
            )
        elif input_format == PROTOCOL_PRONTO:
            code_text = (user_input.get(CONF_PRONTO_CODE) or "").strip()
            timings, _carrier = rf_analysis.parse_pronto(code_text)
        else:  # pragma: no cover - selector restricts the values
            raise rf_analysis.RFInputError("invalid_timings", input_format)

        timings = rf_analysis.normalize(timings)
        repeat_raw = user_input.get(CONF_REPEAT)
        repeat = (
            int(repeat_raw)
            if repeat_raw is not None
            else int(self._code.settings.get(CONF_REPEAT, device.default_repeat))
        )
        # Run the decoder chain so manual entries get the same protocol
        # display (encoding, bits, hex) as learned signals.
        analysis = rf_analysis.analyze(timings)
        extra = {
            key: analysis[key]
            for key in (
                "encoding",
                "bits",
                "bit_count",
                "hex",
                "tristate",
                "protocol_guess",
                "frame_count",
            )
            if key in analysis
        }
        return RFCommandData(
            protocol=input_format,
            frequency=frequency,
            data=timings,
            pulse_length=pulse_length or analysis.get("base_unit_us"),
            code=code_text,
            repeat=repeat,
            source="manual",
            fingerprint=analysis.get("fingerprint"),
            extra=extra,
        )

    async def async_step_slot_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Preview the pending command: accept, test or retry."""
        assert self._pending is not None
        pending = self._pending
        pending_analysis = {
            **pending.extra,
            "pulse_count": len(pending.data),
            "frame_count": pending.extra.get("frame_count", 1),
            "base_unit_us": pending.pulse_length,
            "bit_count": pending.extra.get("bit_count")
            or len(pending.extra.get("bits", ""))
            or None,
            "tristate": pending.extra.get("tristate")
            or (pending.code if pending.protocol == PROTOCOL_TRISTATE else None),
        }
        placeholders = {
            **self._slot_placeholders(),
            "summary": rf_analysis.signal_details(pending_analysis, self._language),
            "timings_preview": rf_analysis.preview_timings(pending.data),
            "source": pending.source,
            "test_note": self._test_note,
        }
        self._test_note = ""
        return self.async_show_menu(
            step_id="slot_confirm",
            menu_options=["slot_accept", "slot_test", "slot_retry"],
            description_placeholders=placeholders,
        )

    async def async_step_slot_test(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send the pending command once for a live test."""
        assert self._pending is not None
        device = self._device()
        rf_manager = get_domain_data(self.hass).rf_manager
        try:
            await rf_manager.async_send(self._pending, device.transmitter)
        except Exception as err:  # noqa: BLE001 - shown inline, flow continues
            self._test_note = f"❌ {err}"
        else:
            self._test_note = "✅"
        return await self.async_step_slot_confirm()

    async def async_step_slot_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discard the pending command.

        With several captured signals, return to the signal list so another
        one can be tried; otherwise re-enter the slot menu.
        """
        self._pending = None
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
        """Store the pending command into the slot and advance."""
        assert self._code is not None and self._pending is not None
        self._code.commands[self._current_slot_key()] = self._pending
        self._pending = None
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
                    slot_label(slot, self._language) for slot in code.commands
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
            for slot, command in code.commands.items():
                analysis = {
                    **command.extra,
                    "pulse_count": len(command.data),
                    "base_unit_us": command.pulse_length,
                    "bit_count": command.extra.get("bit_count")
                    or len(command.extra.get("bits", ""))
                    or None,
                    "tristate": command.extra.get("tristate")
                    or (command.code if command.protocol == PROTOCOL_TRISTATE else None),
                }
                details = rf_analysis.signal_details(analysis, self._language)
                indented = details.replace("\n", "\n  ")
                blocks.append(
                    f"### {slot_label(slot, self._language)}\n  {indented}\n"
                    f"  **Timings:** `{rf_analysis.preview_timings(command.data)}`"
                )
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
                    for command in code.commands.values():
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
            command = code.commands.get(slot)
            try:
                if command is None:
                    raise ValueError(slot)
                await rf_manager.async_send(command, device.transmitter)
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
