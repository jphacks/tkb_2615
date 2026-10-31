"""Compare what a Checkpoint capture took from Rust rows with the Python path, at every save.

Imported by sitecustomize.py of this directory, under the same SHELTERSIM_RULE_SHADOW. For each
save it encodes the Python objects as a capture without rows does and compares the bytes field by
field and item by item, and it runs the restore check's comparison again without what it was told
is known equal, so a row wrongly taken as unchanged shows up. It counts the rows that were not
used, with the reason, because a capture that always falls back passes every hash. The result the
capture goes on with is its own, so the hashes of such a run must still pass.
"""

from __future__ import annotations

import atexit
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any


def install(output: str) -> None:
    from sheltersim import native
    from sheltersim.core import state_mirror
    from sheltersim.core.checkpoint_cache import (
        _WORLD_TREE,
        CheckpointHistoryCache,
        canonical_bytes,
    )
    from sheltersim.core.world import WorldState

    if not native.available():
        return
    seen: Counter[str] = Counter()
    fallbacks: Counter[str] = Counter()
    differences: list[str] = []
    documents = CheckpointHistoryCache.documents
    same_state = WorldState.same_state

    def checked_documents(self: Any, world: Any, metrics: Any) -> Any:
        result = documents(self, world, metrics)
        seen["captures"] += 1
        minute = world.minute_index
        python = world._canonical_data(include_event_log=False)
        if "_core" not in world.__dict__:
            fallbacks["the world has no core"] += 1
            return result
        store = world._core.entities()
        for name in state_mirror.ENTITY_FIELDS:
            raws = self._row_items.get(name)
            if raws is None:
                fallbacks[f"{name}: {store.tainted or 'a value canonical JSON refuses'}"] += 1
                continue
            seen["entity_fields"] += 1
            seen["entity_items"] += len(raws)
            seen["entity_bytes"] += sum(map(len, raws))
            expected = [canonical_bytes(item) for item in python[name]]
            if raws != expected:
                bad = next(
                    (
                        i
                        for i, pair in enumerate(zip(raws, expected, strict=False))
                        if pair[0] != pair[1]
                    ),
                    min(len(raws), len(expected)),
                )
                differences.append(f"minute {minute}: {name}[{bad}]")
        for name, table in (
            *((name, world.model_state) for name in state_mirror.OWNED_NAMESPACES),
            *((name, None) for name in state_mirror.OWNED_FIELDS),
        ):
            if table is not None and name not in table:
                continue
            if name in _WORLD_TREE["model_state"]:
                fallbacks[f"{name}: on the hash tree's path"] += 1
                continue
            if name not in self._owned_rows:
                mirror = world._core.mirror(name)
                reason = mirror.tainted if mirror is not None else "not owned here"
                fallbacks[f"{name}: {reason or 'the field holds another dict'}"] += 1
                continue
            path = (name,) if table is None else ("model_state", name)
            raw = self._fields[path][0]
            value = getattr(world, name) if table is None else table[name]
            seen["owned_rows"] += 1
            seen["owned_bytes"] += len(raw)
            if raw != canonical_bytes(value):
                differences.append(f"minute {minute}: {name}")
        return result

    def checked_same_state(self: Any, other: Any, **known: Any) -> bool:
        result = same_state(self, other, **known)
        rows = known.get("known_equal_rows")
        if rows:
            seen["restore_checks"] += 1
            seen["rows_known_equal"] += sum(map(len, rows.values()))
            seen["rows_restored"] += sum(
                len(getattr(self, name)) - len(ids) for name, ids in rows.items()
            )
            whole = same_state(
                self,
                other,
                known_equal_events=known.get("known_equal_events", 0),
                known_equal_histories=known.get("known_equal_histories"),
            )
            if whole != result:
                differences.append(
                    f"minute {other.minute_index}: the restore check said {result} and {whole}"
                )
        return result

    CheckpointHistoryCache.documents = checked_documents  # type: ignore[method-assign]
    WorldState.same_state = checked_same_state  # type: ignore[method-assign]

    def report() -> None:
        if seen or fallbacks or differences:
            target = Path(output) / f"capture-{os.getpid()}.json"
            document = {
                "seen": {f"capture.{key}": value for key, value in seen.items()},
                "unsupported": {f"capture: {key}": value for key, value in fallbacks.items()},
                "differences": differences[:20],
            }
            target.write_text(json.dumps(document), encoding="utf-8")

    atexit.register(report)
