"""Compare the kernels of native/src/rules/resources.rs with their Python rules at each call.

An operation runs the Python rule on a plain copy of the ledger first, then the kernel on the
world. The two must be the same text of JSON, ledger and resources, and the row must write the
bytes of the tree. Whatever the validator accepts must pass the Python rule.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from shadow_kit import Recorder, pure_shadow, text, values_in

KEY = "resource_ledger_v1"
# A row and a tree are compared at each operation up to here, and at every 32nd after that: their
# bytes take as long as the trail is, and an operation's own comparison reads it all anyway.
VERIFY_EVERY_OPERATION_UP_TO = 2000


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    rules = native.rules("resources")
    state = native.state("resource_ledger")
    recorder = Recorder(output, "resources")
    unsupported = native.Unsupported
    module: list[Any] = []

    def resources() -> Any:
        if not module:
            from sheltersim.domain import resources as loaded

            module.append(loaded)
        return module[0]

    apply_kernel = rules.apply_operation
    validate_kernel = state.validate

    def apply_operation(world: Any, event: Any, operation: Any) -> None:
        recorder.seen["apply.calls"] += 1
        ledger = world.model_state.get(KEY)
        stand_in = SimpleNamespace(
            model_state={KEY: json.loads(json.dumps(ledger))},
            resources=dict(world.resources),
            agents=world.agents,
            zones=world.zones,
            minute_index=world.minute_index,
        )
        error = None
        try:
            resources().apply_resource_operation.__wrapped__(stand_in, event, operation)
        except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
            error = raised
        try:
            apply_kernel(world, event, operation)
        except unsupported as reason:
            recorder.reasons[f"apply: {reason}"] += 1
            if error is None:
                recorder.seen["apply.unsupported_python_accepted"] += 1
            else:
                recorder.seen["apply.unsupported_python_raised"] += 1
            raise
        recorder.seen["apply.kernel_ran"] += 1
        recorder.seen[f"apply.{operation}"] += 1
        tree = world.model_state[KEY]
        if error is not None:
            recorder.differ(f"apply {event.event_id}: the kernel took what Python refused: {error}")
            return
        if text(stand_in.model_state[KEY]) != text(tree):
            recorder.differ(f"apply {event.event_id}: the ledger at {world.minute_index}")
        if text(stand_in.resources) != text(world.resources):
            recorder.differ(f"apply {event.event_id}: the resources at {world.minute_index}")
        recorder.seen["apply.values_compared"] += values_in(tree) + 2
        calls = recorder.seen["apply.kernel_ran"]
        if calls <= VERIFY_EVERY_OPERATION_UP_TO or calls % 32 == 0:
            recorder.seen["apply.rows_verified"] += 1
            if not world._core.mirror(KEY).verify():
                recorder.differ(f"apply {event.event_id}: the row and the tree")

    def validate(world: Any) -> bool:
        accepted = validate_kernel(world)
        recorder.seen[f"validate.{'accepted' if accepted else 'refused'}"] += 1
        if accepted:
            try:
                resources().validate_resource_ledger_state(
                    world.model_state[KEY],
                    world.resources,
                    set(world.agents),
                    set(world.zones),
                    minute_index=world.minute_index,
                )
            except ValueError as raised:
                recorder.differ(f"validate at {world.minute_index}: Python refused: {raised}")
            if recorder.seen["validate.accepted"] % 200 == 0:
                recorder.seen["validate.rows_verified"] += 1
                if not world._core.mirror(KEY).verify():
                    recorder.differ(f"validate at {world.minute_index}: the row and the tree")
        return accepted

    rules.apply_operation = apply_operation
    state.validate = validate
    rules.metric_values = pure_shadow(
        recorder,
        "metric",
        rules.metric_values,
        lambda: resources().resource_metric_values.__wrapped__,
        unsupported,
    )
    rules.reservation_id_was_used = pure_shadow(
        recorder,
        "used",
        rules.reservation_id_was_used,
        lambda: resources().reservation_id_was_used.__wrapped__,
        unsupported,
        detail=lambda result: recorder.seen.update({f"used.{result}": 1}),
    )
