"""Rotation state for ordered RF command variants.

Pure Python (no Home Assistant imports) so it stays testable standalone.
``RFManager`` owns one instance, persists it through a small HA ``Store``
(see ``to_dict``/``load``) and resolves device grouping for
``reset_others``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class CommandRotation:
    """Track the next variant independently for each RF code command.

    The state maps ``(code_id, slot)`` to the index of the variant that the
    next transmission will use. A missing key means "start at variant 0".
    """

    def __init__(self) -> None:
        """Initialize an empty rotation table."""
        self._next_indices: dict[tuple[str, str], int] = {}

    def next_index(self, code_id: str, slot: str, count: int) -> int:
        """Return and advance the next index for one command."""
        if count <= 1:
            self.reset_command(code_id, slot)
            return 0
        key = (code_id, slot)
        index = self._next_indices.get(key, 0) % count
        self._next_indices[key] = (index + 1) % count
        return index

    def rollback(self, code_id: str, slot: str, index: int, count: int) -> bool:
        """Undo ``next_index`` after the send of variant ``index`` failed.

        The variant never went on air, so the next send must use it again;
        otherwise a two-variant remote would repeat the code the receiver
        last accepted. Nothing changes when another send or a reception has
        moved the rotation on in the meantime. Returns whether it rolled back.
        """
        if count <= 1 or not 0 <= index < count:
            return False
        key = (code_id, slot)
        if self._next_indices.get(key, 0) % count != (index + 1) % count:
            return False
        if index:
            self._next_indices[key] = index
        else:
            self._next_indices.pop(key, None)
        return True

    def peek_index(self, code_id: str, slot: str, count: int) -> int:
        """Return the index the next send would use, without advancing."""
        if count <= 1:
            return 0
        return self._next_indices.get((code_id, slot), 0) % count

    def sync_after_receive(
        self, code_id: str, slot: str, received_index: int, count: int
    ) -> None:
        """Set the next index to follow the variant received from a remote."""
        key = (code_id, slot)
        if count <= 1:
            self._next_indices.pop(key, None)
        elif 0 <= received_index < count:
            self._next_indices[key] = (received_index + 1) % count

    def reset_command(self, code_id: str, slot: str) -> None:
        """Reset one command to its first variant."""
        self._next_indices.pop((code_id, slot), None)

    def reset_code(self, code_id: str) -> None:
        """Discard all command rotation state for one RF code."""
        for key in [key for key in self._next_indices if key[0] == code_id]:
            self._next_indices.pop(key, None)

    def reset_others(
        self, code_ids: Iterable[str], keep_code_id: str, keep_slot: str
    ) -> bool:
        """Reset every command of ``code_ids`` except ``(keep_code_id, keep_slot)``.

        Mirrors remotes that restart a button's code sequence as soon as a
        different button is pressed. Returns whether any state changed.
        """
        group = set(code_ids)
        keep = (keep_code_id, keep_slot)
        stale = [
            key for key in self._next_indices if key[0] in group and key != keep
        ]
        for key in stale:
            self._next_indices.pop(key, None)
        return bool(stale)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, dict[str, int]]:
        """Serialize as ``{code_id: {slot: next_index}}`` (JSON-safe)."""
        data: dict[str, dict[str, int]] = {}
        for (code_id, slot), index in sorted(self._next_indices.items()):
            if index:
                data.setdefault(code_id, {})[slot] = index
        return data

    def load(self, data: Any) -> None:
        """Replace the state from ``to_dict`` output, ignoring malformed parts."""
        self._next_indices.clear()
        if not isinstance(data, dict):
            return
        for code_id, slots in data.items():
            if not isinstance(code_id, str) or not isinstance(slots, dict):
                continue
            for slot, index in slots.items():
                if (
                    isinstance(slot, str)
                    and isinstance(index, int)
                    and not isinstance(index, bool)
                    and index > 0
                ):
                    self._next_indices[(code_id, slot)] = index
