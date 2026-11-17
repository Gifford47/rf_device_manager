"""Unit tests for in-memory RF command variant rotation."""

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


if __name__ == "__main__":
    unittest.main()
