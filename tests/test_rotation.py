"""Unit tests for RF command variant rotation (state, reset, persistence)."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest


COMPONENT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "rf_device_manager"
)
_SPEC = importlib.util.spec_from_file_location(
    "command_rotation", COMPONENT / "command_rotation.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
CommandRotation = _MODULE.CommandRotation


class CommandRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rotation = CommandRotation()

    def test_cycles_through_variants_and_wraps(self) -> None:
        self.assertEqual(
            [self.rotation.next_index("code", "on", 4) for _ in range(6)],
            [0, 1, 2, 3, 0, 1],
        )

    def test_each_code_and_command_has_independent_rotation(self) -> None:
        self.assertEqual(self.rotation.next_index("a", "on", 3), 0)
        self.assertEqual(self.rotation.next_index("a", "off", 2), 0)
        self.assertEqual(self.rotation.next_index("b", "on", 3), 0)
        self.assertEqual(self.rotation.next_index("a", "on", 3), 1)
        self.assertEqual(self.rotation.next_index("a", "off", 2), 1)

    def test_receive_synchronizes_next_send(self) -> None:
        self.rotation.sync_after_receive("code", "on", 2, 4)
        self.assertEqual(self.rotation.next_index("code", "on", 4), 3)

    def test_invalid_received_index_does_not_change_rotation(self) -> None:
        self.rotation.sync_after_receive("code", "on", 1, 4)
        self.rotation.sync_after_receive("code", "on", 99, 4)
        self.assertEqual(self.rotation.next_index("code", "on", 4), 2)

    def test_single_variant_does_not_keep_rotation_state(self) -> None:
        self.assertEqual(self.rotation.next_index("code", "on", 1), 0)
        self.assertEqual(self.rotation.next_index("code", "on", 1), 0)
        self.rotation.sync_after_receive("code", "on", 0, 1)
        self.assertEqual(self.rotation.next_index("code", "on", 1), 0)

    def test_reset_command_and_code(self) -> None:
        self.rotation.next_index("code", "on", 3)
        self.rotation.next_index("code", "off", 2)
        self.assertEqual(self.rotation.next_index("code", "on", 3), 1)
        self.rotation.reset_command("code", "on")
        self.assertEqual(self.rotation.next_index("code", "on", 3), 0)
        self.rotation.next_index("code", "off", 2)
        self.rotation.reset_code("code")
        self.assertEqual(self.rotation.next_index("code", "off", 2), 0)

    def test_peek_does_not_advance(self) -> None:
        self.rotation.next_index("code", "on", 3)
        self.assertEqual(self.rotation.peek_index("code", "on", 3), 1)
        self.assertEqual(self.rotation.peek_index("code", "on", 3), 1)
        self.assertEqual(self.rotation.next_index("code", "on", 3), 1)

    def test_reset_others_keeps_only_the_active_command(self) -> None:
        for code_id, slot in (("a", "s1"), ("a", "s2"), ("b", "on"), ("x", "on")):
            self.rotation.next_index(code_id, slot, 4)
        changed = self.rotation.reset_others(["a", "b"], "a", "s1")
        self.assertTrue(changed)
        self.assertEqual(self.rotation.peek_index("a", "s1", 4), 1)
        self.assertEqual(self.rotation.peek_index("a", "s2", 4), 0)
        self.assertEqual(self.rotation.peek_index("b", "on", 4), 0)
        # Codes of other devices are untouched.
        self.assertEqual(self.rotation.peek_index("x", "on", 4), 1)
        self.assertFalse(self.rotation.reset_others(["a", "b"], "a", "s1"))

    def test_persistence_round_trip(self) -> None:
        self.rotation.next_index("code", "speed_1", 4)
        self.rotation.next_index("code", "speed_1", 4)
        self.rotation.next_index("other", "on", 2)
        data = self.rotation.to_dict()
        self.assertEqual(data, {"code": {"speed_1": 2}, "other": {"on": 1}})

        restored = CommandRotation()
        restored.load(data)
        self.assertEqual(restored.next_index("code", "speed_1", 4), 2)
        self.assertEqual(restored.next_index("other", "on", 2), 1)

    def test_wrapped_index_zero_is_not_persisted(self) -> None:
        for _ in range(4):
            self.rotation.next_index("code", "on", 4)
        self.assertEqual(self.rotation.to_dict(), {})

    def test_load_ignores_malformed_data(self) -> None:
        self.rotation.next_index("keep-not", "on", 3)
        self.rotation.load(
            {
                "ok": {"on": 2, "neg": -1, "bool": True, "str": "1"},
                "bad": [1, 2],
                3: {"on": 1},
            }
        )
        self.assertEqual(self.rotation.to_dict(), {"ok": {"on": 2}})
        self.rotation.load(None)
        self.assertEqual(self.rotation.to_dict(), {})

    def test_stale_index_is_clamped_when_variants_shrink(self) -> None:
        self.rotation.load({"code": {"on": 3}})
        # Re-learned with only two variants: index 3 wraps instead of failing.
        self.assertEqual(self.rotation.next_index("code", "on", 2), 1)


if __name__ == "__main__":
    unittest.main()
