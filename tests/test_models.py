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
    # Pure constants needed by the real decoders/rf_analysis modules.
    "IDLE_TRIM_US": 20_000,
    "MIN_SIGNAL_PULSES": 8,
    "PROTOCOL_B0": "b0",
    "PROTOCOL_BINARY": "binary",
    "PROTOCOL_PRONTO": "pronto",
    "PROTOCOL_TRISTATE": "tristate",
}.items():
    setattr(const, name, value)
sys.modules["rfdm.const"] = const

# models imports the real (pure-Python) decoders and rf_analysis modules.
for _name in ("decoders", "rf_analysis"):
    _spec = importlib.util.spec_from_file_location(
        f"rfdm.{_name}", COMPONENT / f"{_name}.py"
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[f"rfdm.{_name}"] = _module
    assert _spec.loader is not None
    _spec.loader.exec_module(_module)
rf_analysis = sys.modules["rfdm.rf_analysis"]

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


class RedecodeTests(unittest.TestCase):
    """Stored commands are re-decoded when DECODER_REVISION increases."""

    def _p8_command(self, bits: str) -> models.RFCommandData:
        timings = rf_analysis.encode_binary(bits, protocol=8) * 4
        analysis = rf_analysis.analyze(timings)
        return models.RFCommandData(
            protocol="raw",
            data=timings,
            pulse_length=analysis["base_unit_us"],
            source="learned:esphome",
            fingerprint=analysis["fingerprint"],
            extra={
                **{
                    key: analysis[key]
                    for key in rf_analysis.STORED_DECODE_KEYS
                    if key in analysis
                },
                "times_seen": 9,
            },
        )

    def test_redecode_is_a_no_op_for_current_decodes(self) -> None:
        cmd = self._p8_command("000101000111000100001001")
        before = cmd.to_dict()
        self.assertFalse(cmd.redecode())
        self.assertEqual(cmd.to_dict(), before)

    def test_redecode_fixes_stale_decode_and_keeps_other_metadata(self) -> None:
        cmd = self._p8_command("000101000111000100001001")
        fresh_fingerprint = cmd.fingerprint
        # Simulate a command learned with the old (inverted) P8 polarity.
        cmd.fingerprint = "24b:stale"
        cmd.extra["bits"] = "111010111000111011110110"
        cmd.extra["hex"] = "0xEB8EF6"
        timings_before = list(cmd.data)

        self.assertTrue(cmd.redecode())
        self.assertEqual(cmd.fingerprint, fresh_fingerprint)
        self.assertEqual(cmd.extra["bits"], "000101000111000100001001")
        self.assertEqual(cmd.extra["times_seen"], 9)  # not a decode key
        self.assertEqual(cmd.data, timings_before)  # timings never change

    def test_redecode_keeps_entered_pulse_length_of_encoded_input(self) -> None:
        timings = rf_analysis.encode_binary("101010101010101010101010", 350, 1)
        cmd = models.RFCommandData(
            protocol="binary", data=timings, pulse_length=350, code="1010"
        )
        cmd.redecode()
        self.assertEqual(cmd.pulse_length, 350)
        self.assertEqual(cmd.extra["bits"], "101010101010101010101010")

    def test_redecode_skips_empty_commands(self) -> None:
        self.assertFalse(models.RFCommandData().redecode())


if __name__ == "__main__":
    unittest.main()
