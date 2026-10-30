"""What a save's complete validation need not read again.

The complete validation before each Checkpoint save reads every append-only history,
and about half of its time is entries it has passed before. Those entries are the ones
compared here: a writer's CheckpointHistoryCache compares each history with the
documents of its last save (native.same_json_items, which tells 1 from True and a tuple
from a list), and only the leading entries exactly equal to them are skipped. A changed,
removed or replaced old entry is therefore noticed by the same save, the whole history
is read again, and the rules decide as they always did.

A rule about an old entry may also read other state. Those inputs are compared too, and
a difference reads the whole history again:

- Service requests and stalls read the known Agent IDs and the facility kinds and zones.
- The body history reads the historical Agent IDs.
- The ledger replay reads the warehouse ID and the known Agent IDs.
- The shared operations replay reads the EventLog (the same object, which only grows,
  or the memo is dropped) and the retained staff operations history.
- All compare minutes with the clock, which going backwards drops the memo.

Everything else the complete validation reads, it still reads at every save. The memo
is held by the writer's cache and only with the rust kernels. A world restored by a
load, fork or resume gets a new cache, and any failed capture drops the memo, so the
first validation after either is complete. Dropping it changes no result.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sheltersim.core.world import WorldState


class SaveValidationMemo:
    """Facts from the last passed complete validation of one writer's saves."""

    def __init__(self) -> None:
        self._minute = -1
        self._event_log: Any = None
        # Leading entries of each history unchanged since they passed, set by begin().
        self._unchanged: dict[Any, int] = {}
        self._inputs: dict[Any, Any] = {}
        # Each replay's state after its last entry, and how many entries that was.
        self._replays: dict[Any, tuple[int, Any]] = {}

    def begin(self, world: WorldState, unchanged: dict[Any, int]) -> None:
        """Start a validation; unchanged maps a history to its entries known unchanged."""
        if world.minute_index < self._minute or world.event_log is not self._event_log:
            self._inputs.clear()
            self._replays.clear()
        self._minute = world.minute_index
        self._event_log = world.event_log
        self._unchanged = unchanged

    def _known(self, path: Any, inputs: Any) -> int:
        known = self._inputs.get(path)
        self._inputs[path] = inputs
        return self._unchanged.get(path, 0) if known == inputs and known is not None else 0

    def unread(self, path: Any, history: Sequence[Any], inputs: Any) -> Sequence[Any]:
        """The entries a rule that reads one entry at a time must read.

        The last unchanged entry is among them, so ordering is checked across the join.
        """
        known = self._known(path, inputs)
        return history[max(known - 1, 0) :] if known else history

    def resume(self, path: Any, inputs: Any) -> tuple[int, Any] | None:
        """A replay's kept state and its entry count, if those entries are all unchanged.

        Older entries dropped from the front change what a replay starts from, and then
        the count no longer matches.
        """
        known = self._known(path, inputs)
        kept = self._replays.pop(path, None)
        return kept if kept is not None and kept[0] == known > 0 else None

    def keep(self, path: Any, count: int, replay: Any) -> None:
        """Keep a replay's state after count entries, which the caller no longer changes."""
        self._replays[path] = (count, replay)
