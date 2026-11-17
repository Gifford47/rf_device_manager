"""Data models for the RF Device Manager integration.

All models are plain dataclasses with tolerant ``from_dict``/``to_dict``
round-tripping so the storage layer can evolve without breaking old data.
The on-disk schema follows the documented format:

Device::

    {"id": "...", "name": "...", "transmitter": "...", "settings": {...}}

Code::

    {"id": "...", "device_id": "...", "name": "...", "entity_type": "...",
     "rf_data": {"commands": {"<slot>": {...}}}, "settings": {...}}
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any
from uuid import uuid4

from .const import (
    CONF_HAS_BRIGHTNESS,
    CONF_HAS_DIRECTION,
    CONF_HAS_OFF,
    CONF_HAS_OSCILLATE,
    CONF_HAS_STOP,
    CONF_OPTIONS,
    CONF_SPEED_COUNT,
    CONF_VALUES,
    DEFAULT_FREQUENCY,
    DEFAULT_REPEAT,
    ENTITY_TYPE_BINARY_SENSOR,
    ENTITY_TYPE_BUTTON,
    ENTITY_TYPE_COVER,
    ENTITY_TYPE_FAN,
    ENTITY_TYPE_LIGHT,
    ENTITY_TYPE_NUMBER,
    ENTITY_TYPE_SELECT,
    ENTITY_TYPE_SENSOR,
    ENTITY_TYPE_SWITCH,
    PROTOCOL_RAW,
)
from .decoders import encoding_label

# ---------------------------------------------------------------------------
# Command slots
# ---------------------------------------------------------------------------

SLOT_ON = "on"
SLOT_OFF = "off"
SLOT_OPEN = "open"
SLOT_CLOSE = "close"
SLOT_STOP = "stop"
SLOT_PRESS = "press"
SLOT_BRIGHTNESS_UP = "brightness_up"
SLOT_BRIGHTNESS_DOWN = "brightness_down"
SLOT_OSCILLATE = "oscillate"
SLOT_DIRECTION_FORWARD = "direction_forward"
SLOT_DIRECTION_REVERSE = "direction_reverse"
SLOT_SPEED_PREFIX = "speed_"
SLOT_OPTION_PREFIX = "option:"
SLOT_VALUE_PREFIX = "value:"

_FIXED_SLOT_LABELS: dict[str, dict[str, str]] = {
    SLOT_ON: {"en": "Turn on", "de": "Einschalten"},
    SLOT_OFF: {"en": "Turn off", "de": "Ausschalten"},
    SLOT_OPEN: {"en": "Open", "de": "Öffnen"},
    SLOT_CLOSE: {"en": "Close", "de": "Schließen"},
    SLOT_STOP: {"en": "Stop", "de": "Stopp"},
    SLOT_PRESS: {"en": "Press", "de": "Tastendruck"},
    SLOT_BRIGHTNESS_UP: {"en": "Brightness up", "de": "Heller"},
    SLOT_BRIGHTNESS_DOWN: {"en": "Brightness down", "de": "Dunkler"},
    SLOT_OSCILLATE: {"en": "Oscillation toggle", "de": "Oszillation umschalten"},
    SLOT_DIRECTION_FORWARD: {"en": "Direction forward", "de": "Drehrichtung vorwärts"},
    SLOT_DIRECTION_REVERSE: {"en": "Direction reverse", "de": "Drehrichtung rückwärts"},
}


def slot_label(slot: str, language: str = "en") -> str:
    """Return a human readable label for a command slot.

    Fixed slots are translated (en/de); dynamic slots embed the user-chosen
    option name, value or speed number.
    """
    lang = "de" if language.lower().startswith("de") else "en"
    if slot in _FIXED_SLOT_LABELS:
        return _FIXED_SLOT_LABELS[slot][lang]
    if slot.startswith(SLOT_SPEED_PREFIX):
        number = slot[len(SLOT_SPEED_PREFIX) :]
        return f"Stufe {number}" if lang == "de" else f"Speed {number}"
    if slot.startswith(SLOT_OPTION_PREFIX):
        name = slot[len(SLOT_OPTION_PREFIX) :]
        return f"Option '{name}'"
    if slot.startswith(SLOT_VALUE_PREFIX):
        value = slot[len(SLOT_VALUE_PREFIX) :]
        return f"Wert {value}" if lang == "de" else f"Value {value}"
    return slot


@dataclass(frozen=True, slots=True)
class SlotSpec:
    """Describes one learnable command slot of an RF code."""

    key: str
    required: bool = True


def format_number(value: float) -> str:
    """Format a float without a trailing ``.0`` for use in slot keys."""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def parse_csv(raw: str) -> list[str]:
    """Split a comma separated user input into stripped, non-empty items."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_slots(entity_type: str, settings: dict[str, Any]) -> list[SlotSpec]:
    """Return the ordered command slots an entity type requires.

    The slot list is derived from the entity type and its type-specific
    settings (e.g. the number of fan speeds or the select options). The
    learning wizard walks this list slot by slot.
    """
    if entity_type == ENTITY_TYPE_SWITCH:
        slots = [SlotSpec(SLOT_ON)]
        if settings.get(CONF_HAS_OFF, True):
            slots.append(SlotSpec(SLOT_OFF))
        return slots

    if entity_type == ENTITY_TYPE_LIGHT:
        slots = [SlotSpec(SLOT_ON)]
        if settings.get(CONF_HAS_OFF, True):
            slots.append(SlotSpec(SLOT_OFF))
        if settings.get(CONF_HAS_BRIGHTNESS, False):
            slots.append(SlotSpec(SLOT_BRIGHTNESS_UP))
            slots.append(SlotSpec(SLOT_BRIGHTNESS_DOWN))
        return slots

    if entity_type == ENTITY_TYPE_FAN:
        speed_count = int(settings.get(CONF_SPEED_COUNT, 3))
        slots = [SlotSpec(SLOT_OFF)]
        slots.extend(
            SlotSpec(f"{SLOT_SPEED_PREFIX}{index}")
            for index in range(1, speed_count + 1)
        )
        if settings.get(CONF_HAS_OSCILLATE, False):
            slots.append(SlotSpec(SLOT_OSCILLATE, required=False))
        if settings.get(CONF_HAS_DIRECTION, False):
            slots.append(SlotSpec(SLOT_DIRECTION_FORWARD, required=False))
            slots.append(SlotSpec(SLOT_DIRECTION_REVERSE, required=False))
        return slots

    if entity_type == ENTITY_TYPE_COVER:
        slots = [SlotSpec(SLOT_OPEN), SlotSpec(SLOT_CLOSE)]
        if settings.get(CONF_HAS_STOP, True):
            slots.append(SlotSpec(SLOT_STOP, required=False))
        return slots

    if entity_type == ENTITY_TYPE_BUTTON:
        return [SlotSpec(SLOT_PRESS)]

    if entity_type == ENTITY_TYPE_SELECT:
        options = settings.get(CONF_OPTIONS, [])
        return [SlotSpec(f"{SLOT_OPTION_PREFIX}{option}") for option in options]

    if entity_type == ENTITY_TYPE_NUMBER:
        values = settings.get(CONF_VALUES, [])
        return [
            SlotSpec(f"{SLOT_VALUE_PREFIX}{format_number(float(value))}")
            for value in values
        ]

    if entity_type == ENTITY_TYPE_SENSOR:
        values = settings.get(CONF_VALUES, [])
        return [SlotSpec(f"{SLOT_VALUE_PREFIX}{value}") for value in values]

    if entity_type == ENTITY_TYPE_BINARY_SENSOR:
        slots = [SlotSpec(SLOT_ON)]
        if settings.get(CONF_HAS_OFF, False):
            slots.append(SlotSpec(SLOT_OFF))
        return slots

    raise ValueError(f"Unknown entity type: {entity_type}")


# ---------------------------------------------------------------------------
# RF command data
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RFCommandData:
    """A single transmittable/matchable RF command.

    ``data`` always holds normalized raw timings (signed, alternating
    microseconds: positive mark, negative space) regardless of the input
    format. ``protocol``, ``code`` and ``pulse_length`` retain the original
    representation for display and editing.
    """

    protocol: str = PROTOCOL_RAW
    frequency: int = DEFAULT_FREQUENCY
    data: list[int] = field(default_factory=list)
    pulse_length: int | None = None
    code: str | None = None
    repeat: int = DEFAULT_REPEAT
    source: str = "manual"
    fingerprint: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage."""
        return {
            "protocol": self.protocol,
            "frequency": self.frequency,
            "data": list(self.data),
            "pulse_length": self.pulse_length,
            "code": self.code,
            "repeat": self.repeat,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RFCommandData:
        """Deserialize from storage, tolerating missing keys."""
        return cls(
            protocol=data.get("protocol", PROTOCOL_RAW),
            frequency=int(data.get("frequency") or DEFAULT_FREQUENCY),
            data=[int(value) for value in data.get("data", [])],
            pulse_length=data.get("pulse_length"),
            code=data.get("code"),
            repeat=int(data.get("repeat", DEFAULT_REPEAT)),
            source=data.get("source", "manual"),
            fingerprint=data.get("fingerprint"),
            extra=dict(data.get("extra", {})),
        )

    def protocol_summary(self) -> str:
        """Return a short protocol summary for entity attributes.

        Examples: ``Space distance (EV1527/PT2262-style) · 0x3332AAAB (63
        bit)`` for decoded commands, ``raw · 131 pulses`` otherwise.
        """
        parts: list[str] = []
        if encoding := self.extra.get("encoding"):
            parts.append(encoding_label(encoding))
        elif self.protocol != PROTOCOL_RAW:
            parts.append(self.protocol)
        else:
            parts.append("raw")

        if hex_code := self.extra.get("hex"):
            bit_count = self.extra.get("bit_count") or len(
                self.extra.get("bits", "")
            )
            parts.append(f"{hex_code} ({bit_count} bit)" if bit_count else hex_code)
        elif self.code:
            parts.append(self.code)
        else:
            parts.append(f"{len(self.data)} pulses")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# RF code (one code == one entity with 1..n command slots)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RFCode:
    """An RF code definition that materializes as one Home Assistant entity."""

    id: str
    device_id: str
    name: str
    entity_type: str
    description: str = ""
    enabled: bool = True
    commands: dict[str, list[RFCommandData]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        device_id: str,
        name: str,
        entity_type: str,
        *,
        description: str = "",
        settings: dict[str, Any] | None = None,
    ) -> RFCode:
        """Create a new code with a fresh unique id."""
        return cls(
            id=uuid4().hex,
            device_id=device_id,
            name=name,
            entity_type=entity_type,
            description=description,
            settings=dict(settings or {}),
        )

    @property
    def unique_id(self) -> str:
        """Return the entity registry unique id for this code."""
        return f"{self.device_id}_{self.id}"

    def primary_slot(self) -> str | None:
        """Return the first configured command slot, if any."""
        return next(iter(self.commands), None)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage using the documented schema."""
        return {
            "id": self.id,
            "device_id": self.device_id,
            "name": self.name,
            "entity_type": self.entity_type,
            "description": self.description,
            "enabled": self.enabled,
            "rf_data": {
                "commands": {
                    slot: [command.to_dict() for command in variants]
                    for slot, variants in self.commands.items()
                }
            },
            "settings": dict(self.settings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RFCode:
        """Deserialize from storage, tolerating missing keys."""
        commands_raw = (data.get("rf_data") or {}).get("commands", {})
        return cls(
            id=data["id"],
            device_id=data["device_id"],
            name=data.get("name", ""),
            entity_type=data.get("entity_type", ENTITY_TYPE_SWITCH),
            description=data.get("description", ""),
            enabled=bool(data.get("enabled", True)),
            commands={
                slot: [
                    RFCommandData.from_dict(command)
                    for command in (
                        raw_variants
                        if isinstance(raw_variants, list)
                        else [raw_variants]
                    )
                    if isinstance(command, dict)
                ]
                for slot, raw_variants in commands_raw.items()
            },
            settings=dict(data.get("settings", {})),
        )

    def duplicate(self, new_device_id: str) -> RFCode:
        """Return a copy of this code bound to another device."""
        clone = RFCode.from_dict(self.to_dict())
        clone.id = uuid4().hex
        clone.device_id = new_device_id
        return clone


# ---------------------------------------------------------------------------
# RF device
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RFDeviceConfig:
    """Configuration of a managed RF device (one config entry each)."""

    id: str
    name: str
    transmitter: str = ""
    description: str = ""
    frequency: int = DEFAULT_FREQUENCY
    default_repeat: int = DEFAULT_REPEAT
    enabled: bool = True
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, name: str, **kwargs: Any) -> RFDeviceConfig:
        """Create a new device config with a fresh unique id."""
        return cls(id=uuid4().hex, name=name, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage using the documented schema."""
        return {
            "id": self.id,
            "name": self.name,
            "transmitter": self.transmitter,
            "settings": {
                "description": self.description,
                "frequency": self.frequency,
                "default_repeat": self.default_repeat,
                "enabled": self.enabled,
                **self.settings,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RFDeviceConfig:
        """Deserialize from storage, tolerating missing keys."""
        settings = dict(data.get("settings", {}))
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            transmitter=data.get("transmitter", ""),
            description=settings.pop("description", ""),
            frequency=int(settings.pop("frequency", DEFAULT_FREQUENCY)),
            default_repeat=int(settings.pop("default_repeat", DEFAULT_REPEAT)),
            enabled=bool(settings.pop("enabled", True)),
            settings=settings,
        )


# ---------------------------------------------------------------------------
# Captured signals (learning)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CapturedSignal:
    """A raw RF capture received while learning."""

    timings: list[int]
    source: str
    frequency: int | None = None
    received_at: float = field(default_factory=time.time)
    analysis: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str | None:
        """Return the analysis fingerprint, if the signal was analyzed."""
        return self.analysis.get("fingerprint")

    def to_dict(self) -> dict[str, Any]:
        """Serialize for service responses and diagnostics."""
        return {
            "timings": list(self.timings),
            "source": self.source,
            "frequency": self.frequency,
            "received_at": self.received_at,
            "analysis": dict(self.analysis),
        }
