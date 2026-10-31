"""Compare every kernel of native/src/rules/action_lifecycle.rs with its Python rule at each call.

Each call runs the Python rule first, on a copy of the open actions and on a world that only
collects the records it would append. Then the kernel runs on the real world. They must give:

- the same receipts: the bytes the log holds against json.dumps of the Python record, and the
  object Python makes from those bytes against that record;
- the same event counter, so the same IDs and sequences to whatever comes next;
- the same open actions, with their keys in the same order.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from types import SimpleNamespace
from typing import Any

from shadow_kit import Recorder, text, values_in

KERNELS = (
    "record_action_proposal",
    "record_applied_action_completion",
    "record_applied_population_exit",
    "finalize_active_actions",
)


class _CollectingLog:
    """What a Python rule asks of the log, with what it appends kept apart."""

    def __init__(self, log: Any) -> None:
        self.log = log
        self.appended: list[Any] = []

    def find(self, event_id: str) -> Any:
        return self.log.find(event_id)

    def append(self, record: Any) -> None:
        if self.log.contains_id(record.event.event_id):
            raise ValueError(f"event {record.event.event_id!r} already realized")
        if self.log.contains_sequence(record.event.sequence):
            raise ValueError(f"event sequence {record.event.sequence} already realized")
        self.appended.append(record)


class _Stub(SimpleNamespace):
    def allocate_event_sequence(self) -> int:
        sequence = self.counter
        self.counter = sequence + 1
        return sequence


def _python_json(record: Any) -> bytes:
    return json.dumps(
        record.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    rules = native.rules("action_lifecycle")
    recorder = Recorder(output, "action_lifecycle")
    unsupported = native.Unsupported
    module: list[Any] = []

    def python_rule(name: str) -> Any:
        if not module:
            from sheltersim.domain import action_lifecycle

            module.append(action_lifecycle)
        return getattr(module[0], name).__wrapped__

    def shadow(name: str, kernel: Any) -> Any:
        def run(world: Any, *arguments: Any, active_actions: Any, **options: Any) -> None:
            recorder.seen[f"{name}.calls"] += 1
            plain = copy.deepcopy(active_actions.snapshot())
            log = world.event_log
            before, counter = len(log), world.id_counters.get("event", 0)
            twin = _CollectingLog(log)
            stub = _Stub(
                agents=world.agents,
                model_state=world.model_state,
                minute_index=world.minute_index,
                event_log=twin,
                counter=counter,
            )
            error = None
            try:
                python_rule(name)(stub, *arguments, active_actions=plain, **options)
            except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
                error = raised
            try:
                kernel(world, *arguments, active_actions=active_actions, **options)
            except unsupported as reason:
                recorder.reasons[f"{name}: {reason}"] += 1
                raise
            recorder.seen[f"{name}.kernel_ran"] += 1
            where = f"{name} at {world.minute_index}"
            if error is not None:
                recorder.differ(f"{where}: the kernel took what Python turned down: {error}")
                return
            raws = log.items_bytes(before, len(log))
            if len(raws) != len(twin.appended):
                recorder.differ(f"{where}: {len(raws)} receipts, not {len(twin.appended)}")
                return
            branches: Counter[str] = Counter()
            for raw, record in zip(raws, twin.appended, strict=True):
                if raw != _python_json(record):
                    recorder.differ(f"{where}: {raw!r:.300} is not {_python_json(record)!r:.300}")
                    continue
                document = record.to_dict()
                recorder.seen[f"{name}.values_compared"] += values_in(document)
                recorder.seen[f"{name}.bytes_compared"] += len(raw)
                made = log.find(record.event.event_id).to_dict()
                if made != document:
                    recorder.differ(f"{where}: the object of the bytes is not the record")
                branches[document["event"]["payload"]["transition"]] += 1
                if document["event"]["causation_id"] is not None:
                    branches["with_cause"] += 1
                if document["event"]["payload"]["activity"] is None:
                    branches["no_activity"] += 1
            recorder.seen.update({f"{name}.{key}": n for key, n in branches.items()})
            if world.id_counters.get("event", 0) != stub.counter:
                recorder.differ(f"{where}: the event counter is not {stub.counter}")
            mine = active_actions.snapshot()
            if text(mine) != text(plain):
                recorder.differ(f"{where}: the open actions differ")
            recorder.seen[f"{name}.values_compared"] += values_in(plain)

        return run

    for name in KERNELS:
        setattr(rules, name, shadow(name, getattr(rules, name)))
