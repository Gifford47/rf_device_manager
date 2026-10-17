"""Constants for the RF Device Manager integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "rf_device_manager"

MANUFACTURER = "RF Device Manager"
MODEL = "Virtual RF device"

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1
STORAGE_MINOR_VERSION = 2
STORAGE_SAVE_DELAY = 1.0  # seconds, debounce for Store.async_delay_save

# ---------------------------------------------------------------------------
# Platforms handled by this integration
# ---------------------------------------------------------------------------

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.COVER,
    Platform.FAN,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Entity types selectable when creating an RF code. Values double as the
# Home Assistant platform name the resulting entity is created on.
ENTITY_TYPE_SWITCH = "switch"
ENTITY_TYPE_LIGHT = "light"
ENTITY_TYPE_FAN = "fan"
ENTITY_TYPE_COVER = "cover"
ENTITY_TYPE_BUTTON = "button"
ENTITY_TYPE_SELECT = "select"
ENTITY_TYPE_NUMBER = "number"
ENTITY_TYPE_SENSOR = "sensor"
ENTITY_TYPE_BINARY_SENSOR = "binary_sensor"

ENTITY_TYPES: list[str] = [
    ENTITY_TYPE_SWITCH,
    ENTITY_TYPE_LIGHT,
    ENTITY_TYPE_FAN,
    ENTITY_TYPE_COVER,
    ENTITY_TYPE_BUTTON,
    ENTITY_TYPE_SELECT,
    ENTITY_TYPE_NUMBER,
    ENTITY_TYPE_SENSOR,
    ENTITY_TYPE_BINARY_SENSOR,
]

# Entity types that only receive (match incoming RF signals) and never send.
RECEIVE_ONLY_TYPES: frozenset[str] = frozenset(
    {ENTITY_TYPE_SENSOR, ENTITY_TYPE_BINARY_SENSOR}
)

# ---------------------------------------------------------------------------
# Config / options keys
# ---------------------------------------------------------------------------

CONF_DEVICE_ID = "device_id"
CONF_NAME = "name"
CONF_DESCRIPTION = "description"
CONF_TRANSMITTER = "transmitter"
CONF_FREQUENCY = "frequency"
CONF_FREQUENCY_PRESET = "frequency_preset"
CONF_FREQUENCY_CUSTOM = "frequency_custom"
CONF_DEFAULT_REPEAT = "default_repeat"
CONF_ENABLED = "enabled"
CONF_DUPLICATE_FROM = "duplicate_from"
CONF_SOURCE_ENTRY = "source_entry"

CONF_ENTITY_TYPE = "entity_type"
CONF_ICON = "icon"
CONF_REPEAT = "repeat"

# RF code / manual input keys
CONF_INPUT_FORMAT = "input_format"
CONF_RAW_TIMINGS = "raw_timings"
CONF_TRISTATE_CODE = "tristate_code"
CONF_BINARY_CODE = "binary_code"
CONF_PRONTO_CODE = "pronto_code"
CONF_PULSE_LENGTH = "pulse_length"
CONF_RC_PROTOCOL = "rc_protocol"
CONF_B0_CODE = "b0_code"

# Type-specific settings keys
CONF_HAS_OFF = "has_off"
CONF_HAS_STOP = "has_stop"
CONF_HAS_BRIGHTNESS = "has_brightness"
CONF_BRIGHTNESS_STEPS = "brightness_steps"
CONF_SPEED_COUNT = "speed_count"
CONF_HAS_OSCILLATE = "has_oscillate"
CONF_HAS_DIRECTION = "has_direction"
CONF_OPTIONS = "options"
CONF_VALUES = "values"
CONF_UNIT = "unit_of_measurement"
CONF_DEVICE_CLASS = "device_class"
CONF_OFF_DELAY = "off_delay"
CONF_NUMBER_MODE = "mode"

# ---------------------------------------------------------------------------
# RF data formats stored in a command's ``protocol`` field
# ---------------------------------------------------------------------------

PROTOCOL_RAW = "raw"
PROTOCOL_TRISTATE = "tristate"
PROTOCOL_BINARY = "binary"
PROTOCOL_PRONTO = "pronto"
PROTOCOL_B0 = "b0"  # Tasmota RfRaw B0/B1 bucket format

INPUT_FORMATS: list[str] = [
    PROTOCOL_RAW,
    PROTOCOL_B0,
    PROTOCOL_TRISTATE,
    PROTOCOL_BINARY,
    PROTOCOL_PRONTO,
]

# ---------------------------------------------------------------------------
# Frequencies
# ---------------------------------------------------------------------------

DEFAULT_FREQUENCY = 433_920_000  # Hz

# Common sub-GHz remote control frequencies, offered as presets in the UI.
FREQUENCY_PRESETS: dict[str, int] = {
    "300.00 MHz": 300_000_000,
    "303.87 MHz": 303_870_000,
    "310.00 MHz": 310_000_000,
    "315.00 MHz": 315_000_000,
    "318.00 MHz": 318_000_000,
    "390.00 MHz": 390_000_000,
    "418.00 MHz": 418_000_000,
    "433.42 MHz": 433_420_000,
    "433.92 MHz": 433_920_000,
    "868.30 MHz": 868_300_000,
    "868.35 MHz": 868_350_000,
    "915.00 MHz": 915_000_000,
}

FREQUENCY_PRESET_CUSTOM = "custom"

# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------

DEFAULT_LEARN_TIMEOUT = 30.0  # seconds to wait for the first signal
LEARN_COLLECT_WINDOW = 4.0  # seconds to keep collecting after the first signal
LEARN_MAX_SIGNALS = 8  # distinct signals kept per learning session
MIN_SIGNAL_PULSES = 8  # ignore captures shorter than this many edges

# Extra repeats when sending a stored command; 0 sends exactly once.
# Real remotes transmit their frame continuously while a button is held, and
# typical OOK receivers only accept a code after several identical frames.
# 7 extra repeats = 8 frames on air (~0.5-1 s), matching a short button press.
# (Before storage 1.2 / entry 1.2, 0 meant "use 7"; see the migrations.)
DEFAULT_REPEAT = 7

# Interval used by the receiver hub to (re-)attach to ESPHome API clients,
# picking up reconnects and newly added nodes.
RECEIVER_MAINTENANCE_INTERVAL = 30  # seconds

# ---------------------------------------------------------------------------
# Events fired on the Home Assistant bus
# ---------------------------------------------------------------------------

EVENT_CODE_RECEIVED = f"{DOMAIN}_code_received"
EVENT_CODE_LEARNED = f"{DOMAIN}_code_learned"

# Fallback learn source: users can forward raw captures from any ESPHome node
# via ``homeassistant.event`` with this event type and ``timings`` in the data.
ESPHOME_FALLBACK_EVENT = "esphome.rf_code_received"

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

SERVICE_SEND_CODE = "send_code"
SERVICE_SEND_RAW = "send_raw"
SERVICE_LEARN_CODE = "learn_code"

ATTR_CODE = "code"
ATTR_COMMAND = "command"
ATTR_REPEAT = "repeat"
ATTR_TRANSMITTER = "transmitter"
ATTR_FREQUENCY = "frequency"
ATTR_TIMINGS = "timings"
ATTR_TIMEOUT = "timeout"
ATTR_DEVICE_ID = "device_id"

# ---------------------------------------------------------------------------
# Repairs
# ---------------------------------------------------------------------------

ISSUE_MISSING_TRANSMITTER = "missing_transmitter"

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

RADIO_FREQUENCY_DOMAIN = "radio_frequency"
ESPHOME_DOMAIN = "esphome"

# Signals with a leading/trailing gap longer than this are trimmed before
# storing/sending; such idle periods are not part of the RF code.
IDLE_TRIM_US = 20_000
