"""In-memory rotation state for ordered RF command variants."""

from __future__ import annotations


class CommandRotation:
    """Track the next variant independently for each RF code command."""

    def __init__(self) -> None:
        """Initialize an empty, non-persistent rotation table."""
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
