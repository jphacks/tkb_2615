"""A saved list held as the canonical JSON bytes of its items (docs/rust-port.md, B1).

A Checkpoint read back from an archive keeps its EventLog documents this way: the
chunk bytes are split into item bytes, the hashes are computed from them, and the
restored EventLog is built from them in Rust. An item is parsed only when Python
reads it, and every read parses it anew, so no reader can change another's value.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any, overload


class SavedItems(Sequence[Any]):
    """A read-only list of JSON values, each kept as its canonical JSON bytes."""

    __slots__ = ("raws",)
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, raws: list[bytes]) -> None:
        self.raws = raws

    def __len__(self) -> int:
        return len(self.raws)

    @overload
    def __getitem__(self, index: int) -> Any: ...

    @overload
    def __getitem__(self, index: slice) -> list[Any]: ...

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [json.loads(raw) for raw in self.raws[index]]
        return json.loads(self.raws[index])

    def __iter__(self) -> Iterator[Any]:
        return (json.loads(raw) for raw in self.raws)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SavedItems):
            return self.raws == other.raws
        if isinstance(other, list):
            return len(other) == len(self.raws) and all(
                json.loads(raw) == value for raw, value in zip(self.raws, other, strict=True)
            )
        return NotImplemented

    def __copy__(self) -> SavedItems:
        return SavedItems(list(self.raws))

    def __deepcopy__(self, memo: dict[int, Any]) -> SavedItems:
        # The bytes are immutable, so a copy shares them.
        return SavedItems(list(self.raws))

    def __reduce__(self) -> tuple[Any, ...]:
        return SavedItems, (self.raws,)

    def __repr__(self) -> str:
        return f"SavedItems({len(self.raws)} items)"


def json_default(value: Any) -> Any:
    """json.dumps(default=...) for documents that may hold SavedItems."""
    if isinstance(value, SavedItems):
        return list(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
