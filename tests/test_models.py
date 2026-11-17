"""Regression tests for RF command variant storage compatibility.

Run from the repository root with::

    python tests/test_models.py

The models are plain dataclasses; tiny module stubs keep this test independent
of a full Home Assistant installation.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "rf_device_manager"

package = types.ModuleType("rfdm")
package.__path__ = [str(COMPONENT)]
sys.modules["rfdm"] = package

const = types.ModuleType("rfdm.const")
for name, value in {
    "CONF_HAS_BRIGHTNESS": "has_brightness",
    "CONF_HAS_DIRECTION": "has_direction",
    "CONF_HAS_OFF": "has_off",
    "CONF_HAS_OSCILLATE": "has_oscillate",
    "CONF_HAS_STOP": "has_stop",
    "CONF_OPTIONS": "options",
    "CONF_SPEED_COUNT": "speed_count",
    "CONF_VALUES": "values",
    "DEFAULT_FREQUENCY": 433_920_000,
    "DEFAULT_REPEAT": 10,
    "ENTITY_TYPE_BINARY_SENSOR": "binary_sensor",
    "ENTITY_TYPE_BUTTON": "button",
    "ENTITY_TYPE_COVER": "cover",
    "ENTITY_TYPE_FAN": "fan",
    "ENTITY_TYPE_LIGHT": "light",
    "ENTITY_TYPE_NUMBER": "number",
    "ENTITY_TYPE_SELECT": "select",
    "ENTITY_TYPE_SENSOR": "sensor",
    "ENTITY_TYPE_SWITCH": "switch",
    "PROTOCOL_RAW": "raw",
}.items():
    setattr(const, name, value)
sys.modules["rfdm.const"] = const

decoders = types.ModuleType("rfdm.decoders")
decoders.encoding_label = lambda value: value
sys.modules["rfdm.decoders"] = decoders

spec = importlib.util.spec_from_file_location(
    "rfdm.models", COMPONENT / "models.py"
)
models = importlib.util.module_from_spec(spec)
sys.modules["rfdm.models"] = models
assert spec.loader is not None
spec.loader.exec_module(models)


def command(data: list[int], fingerprint: str) -> dict:
    """Return a minimal serialized RF command."""
    return {
        "protocol": "raw",
        "frequency": 433_920_000,
        "data": data,
        "repeat": 10,
        "fingerprint": fingerprint,
        "extra": {"bits": fingerprint},
    }


def code_record(commands: dict) -> dict:
    """Return a minimal stored RF code."""
    return {
        "id": "code-id",
        "device_id": "device-id",
        "name": "Test",
        "entity_type": "switch",
        "rf_data": {"commands": commands},
    }


class RFCommandVariantStorageTests(unittest.TestCase):
    def test_legacy_single_command_is_loaded_as_one_variant(self) -> None:
        code = models.RFCode.from_dict(
            code_record({"on": command([100, -300], "1010")})
        )

        self.assertEqual(len(code.commands["on"]), 1)
        self.assertEqual(code.commands["on"][0].fingerprint, "1010")
        self.assertEqual(
            code.to_dict()["rf_data"]["commands"]["on"][0]["data"],
            [100, -300],
        )

    def test_multiple_variants_round_trip(self) -> None:
        code = models.RFCode.from_dict(
            code_record(
                {
                    "on": [
                        command([100, -300], "1010"),
                        command([110, -310], "1011"),
                    ],
                    "off": [command([200, -400], "0011")],
                }
            )
        )

        self.assertEqual(
            [item.fingerprint for item in code.commands["on"]],
            ["1010", "1011"],
        )
        serialized = code.to_dict()["rf_data"]["commands"]
        self.assertIsInstance(serialized["on"], list)
        self.assertEqual(len(serialized["on"]), 2)
        restored = models.RFCode.from_dict(code.to_dict())
        self.assertEqual(
            [item.data for item in restored.commands["on"]],
            [[100, -300], [110, -310]],
        )

    def test_duplicate_preserves_all_variants(self) -> None:
        code = models.RFCode.from_dict(
            code_record(
                {"on": [command([100, -300], "1010"), command([110, -310], "1011")]}
            )
        )

        duplicate = code.duplicate("other-device")

        self.assertEqual(duplicate.device_id, "other-device")
        self.assertEqual(
            [item.fingerprint for item in duplicate.commands["on"]],
            ["1010", "1011"],
        )


if __name__ == "__main__":
    unittest.main()
