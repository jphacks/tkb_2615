"""Compare every kernel of native/src/rules/staff.rs with its Python rule at each call.

A kernel runs on the world, and when it takes the call the Python rule runs on a twin of the world:
copies of what the rule writes, and every event counter closed. A Python rule that would have
started something, which the kernel must have refused, reaches `allocate_event_sequence` first and
is reported. The validator is compared on what it accepts: the Python rule must accept that too.
"""

from __future__ import annotations

import json
from dataclasses import fields
from types import SimpleNamespace
from typing import Any

from shadow_kit import Recorder, text, values_in

KEY = "staff_operations_v1"
ROW_CHECK_EVERY = 25


class _WouldStart(Exception):
    """The Python rule reached what only a rule that starts something reaches."""


class _Twin:
    """The world for a Python rule: its other parts are the world's, and it can't start anything."""

    def __init__(self, world: Any, **parts: Any) -> None:
        self.__dict__.update(parts, _world=world)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_world"], name)

    def allocate_event_sequence(self) -> int:
        raise _WouldStart

    def schedule_event(self, _event: Any) -> None:
        raise _WouldStart


def _no_route(*_arguments: Any) -> None:
    raise _WouldStart


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    rules = native.rules("staff")
    recorder = Recorder(output, "staff")
    unsupported = native.Unsupported
    module: dict[str, Any] = {}

    def staff() -> Any:
        if "staff" not in module:
            from sheltersim.domain import staff as loaded

            module["staff"] = loaded
        return module["staff"]

    def operations() -> Any:
        if "operations" not in module:
            from sheltersim.domain import staff_operations as loaded

            module["operations"] = loaded
        return module["operations"]

    update_kernel = rules.update_operations
    assign_kernel = rules.assign_operations
    relief_kernel = rules.relief
    pending_kernel = rules.assign_none
    snapshot_kernel = rules.wait_snapshot

    def update_operations(world: Any, minute: Any) -> None:
        recorder.seen["update.calls"] += 1
        state = world.model_state.get(KEY)
        if state is None:
            recorder.seen["update.no_namespace"] += 1
            update_kernel(world, minute)
            return
        before = json.loads(json.dumps(state))
        order = list(state["zones"])
        try:
            update_kernel(world, minute)
        except unsupported as reason:
            recorder.reasons[f"update: {reason}"] += 1
            raise
        recorder.seen["update.kernel_ran"] += 1
        table = dict(world.model_state)
        table[KEY] = before
        operations().update_staff_operations.__wrapped__(_Twin(world, model_state=table), minute)
        state = world.model_state[KEY]
        if text(before) != text(state) or order != list(state["zones"]):
            recorder.differ(f"update at {minute}: {text(before)[:300]} and {text(state)[:300]}")
        recorder.seen["update.values_compared"] += values_in(before)
        if (
            recorder.seen["update.calls"] % ROW_CHECK_EVERY == 0
            and not world._core.mirror(KEY).verify()
        ):
            recorder.differ(f"update at {minute}: the row and the tree")
        recorder.seen["update.zones_with_people"] += sum(
            zone["sanitation"] < 1.0 for zone in state["zones"].values()
        )

    def read_only(label: str, kernel: Any, rule: Any) -> Any:
        def shadow(world: Any, minute: Any, *rest: Any) -> Any:
            recorder.seen[f"{label}.calls"] += 1
            try:
                result = kernel(world, minute, *rest)
            except unsupported as reason:
                recorder.reasons[f"{label}: {reason}"] += 1
                raise
            recorder.seen[f"{label}.kernel_ran"] += 1
            try:
                expected = rule().__wrapped__(_Twin(world), minute, *rest)
            except _WouldStart:
                recorder.differ(f"{label} at {minute}: the Python rule starts something")
                return result
            if expected != result:
                recorder.differ(f"{label} at {minute}: {result!r} and {expected!r}")
            recorder.seen[f"{label}.values_compared"] += 1
            return result

        return shadow

    def assign_pending(service: Any, minute: Any, *, validate_state: bool = True) -> Any:
        recorder.seen["pending.calls"] += 1
        world = service.world
        metrics_before = dict(world.metric_state)
        requests = list(world.unsettled_support_requests())
        copies = {item.id: replace_request(item) for item in requests}
        try:
            result = pending_kernel(service, minute, validate_state=validate_state)
        except unsupported as reason:
            recorder.reasons[f"pending: {reason}"] += 1
            raise
        recorder.seen["pending.kernel_ran"] += 1
        twin_world = _Twin(
            world,
            metric_state=metrics_before,
            support_requests=copies,
            unsettled_support_requests=lambda: list(copies.values()),
        )
        twin = SimpleNamespace(
            world=twin_world,
            fairness_after_minutes=service.fairness_after_minutes,
            route_resolver=_no_route,
            travel_scheduler=_no_route,
            task_duration_minutes=service.task_duration_minutes,
            policy_version=service.policy_version,
        )
        try:
            expected = staff().StaffService.assign_pending.__wrapped__(
                twin, minute, validate_state=False
            )
        except _WouldStart:
            recorder.differ(f"pending at {minute}: the Python rule starts something")
            return result
        if expected != result:
            recorder.differ(f"pending at {minute}: {result!r} and {expected!r}")
        fresh = dict(world.metric_state)
        if text(metrics_before) != text(fresh):
            recorder.differ(f"pending at {minute}: metrics differ")
        recorder.seen["pending.values_compared"] += len(fresh)
        waiting = 0
        for item in requests:
            copy = copies[item.id]
            first = (item.last_assignment_attempt_minute, item.last_assignment_wait_reason_code)
            second = (copy.last_assignment_attempt_minute, copy.last_assignment_wait_reason_code)
            if text(first) != text(second):
                recorder.differ(f"pending at {minute}: {item.id} has {first} and {second}")
            recorder.seen["pending.values_compared"] += 2
            waiting += item.status.value == "pending"
        recorder.seen["pending.requests_waiting"] += waiting
        return result

    def replace_request(item: Any) -> Any:
        from sheltersim.domain.models import SupportRequestState

        return SupportRequestState(
            **{field.name: getattr(item, field.name) for field in fields(SupportRequestState)}
        )

    def wait_snapshot(world: Any, active_support: Any, sample_minute: Any) -> Any:
        recorder.seen["snapshot.calls"] += 1
        try:
            result = snapshot_kernel(world, active_support, sample_minute)
        except unsupported as reason:
            recorder.reasons[f"snapshot: {reason}"] += 1
            raise
        recorder.seen["snapshot.kernel_ran"] += 1
        from sheltersim.experiments import metrics

        expected = metrics._support_wait_snapshot.__wrapped__(world, active_support, sample_minute)
        if text(expected) != text(result):
            recorder.differ(
                f"snapshot at {sample_minute}: {text(result)[:300]} and {text(expected)[:300]}"
            )
        else:
            recorder.seen["snapshot.values_compared"] += values_in(expected[0]) + len(expected[1])
            for detail in expected[0].values():
                for reason_code in detail["reason_codes"]:
                    recorder.seen[f"snapshot.{reason_code}"] += 1
            recorder.seen["snapshot.no_qualified_people"] += len(expected[1])
        return result

    rules.update_operations = update_operations
    rules.assign_operations = read_only(
        "assign", assign_kernel, lambda: operations().assign_staff_operations
    )
    rules.relief = read_only(
        "relief", relief_kernel, lambda: operations().schedule_staff_operation_relief
    )
    rules.assign_none = assign_pending
    rules.wait_snapshot = wait_snapshot

    # domain/staff_operations.py registers `validate` when it is imported, later than this.
    state_rules = native.state("staff_operations")
    rust_validate = state_rules.validate

    def validator(world: Any) -> bool:
        accepted = rust_validate(world)
        recorder.seen["validator_accepted" if accepted else "validator_refused"] += 1
        try:
            operations().validate_staff_operations_state(world)
        except ValueError as error:
            if accepted:
                recorder.differ(f"the validator took what Python turned down: {error}")
        else:
            if not accepted:
                recorder.seen["validator_refused_what_python_passes"] += 1
        return accepted

    state_rules.validate = validator
    _prepare_common(native, Recorder(output, "common"))


SHARED = "shared_operations_v1"


def _comparable(state: dict[str, Any], tail: int) -> dict[str, Any]:
    """The namespace with the last items of its history, in plain containers."""
    history = state["history"]
    return {
        **json.loads(json.dumps({key: value for key, value in state.items() if key != "history"})),
        "history": json.loads(json.dumps(history[-tail:] if tail else [])),
    }


def _prepare_common(native: Any, recorder: Recorder) -> None:
    rules = native.rules("common_operations")
    state_rules = native.state("shared_operations")
    unsupported = native.Unsupported
    record_kernel = rules.record_shared_operation
    tick_kernel = state_rules.validate_tick
    module: list[Any] = []

    def common() -> Any:
        if not module:
            from sheltersim.domain import common_operations

            module.append(common_operations)
        return module[0]

    def record_shared_operation(world: Any, record: Any) -> None:
        recorder.seen["record.calls"] += 1
        state = world.model_state.get(SHARED)
        if not isinstance(state, dict) or not isinstance(state.get("history"), list):
            recorder.seen["record.no_namespace"] += 1
            record_kernel(world, record)
            return
        # The Python rule reads the recent records only, unless the list is at its limit.
        size = len(state["history"])
        tail = size if size >= 19_999 else min(size, 64)
        before = _comparable(state, tail)
        try:
            record_kernel(world, record)
        except unsupported as reason:
            recorder.reasons[f"record: {reason}"] += 1
            raise
        recorder.seen["record.kernel_ran"] += 1
        recorder.seen[f"record.{record.status}"] += 1
        recorder.seen["record.with_state_changes"] += bool(record.state_changes)
        table = dict(world.model_state)
        table[SHARED] = before
        try:
            common().record_shared_operation.__wrapped__(_Twin(world, model_state=table), record)
        except ValueError as error:
            recorder.differ(f"record: the kernel took what Python turned down: {error}")
            return
        state = world.model_state[SHARED]
        after = _comparable(state, len(before["history"]))
        if text(before) != text(after):
            recorder.differ(
                f"record of {record.event_id}: {text(before)[-400:]} and {text(after)[-400:]}"
            )
        recorder.seen["record.values_compared"] += values_in(before)
        if (
            recorder.seen["record.calls"] % ROW_CHECK_EVERY == 0
            and not world._core.mirror(SHARED).verify()
        ):
            recorder.differ(f"record of {record.event_id}: the row and the tree")

    inside = [False]

    def validate_tick(world: Any) -> bool:
        if inside[0]:
            return False
        accepted = tick_kernel(world)
        recorder.seen["tick_validator_accepted" if accepted else "tick_validator_refused"] += 1
        inside[0] = True
        try:
            common().validate_shared_operations_state(world, validate_event_history=False)
        except ValueError as error:
            if accepted:
                recorder.differ(f"the tick validator took what Python turned down: {error}")
        else:
            if not accepted:
                recorder.seen["tick_validator_refused_what_python_passes"] += 1
        finally:
            inside[0] = False
        return accepted

    rules.record_shared_operation = record_shared_operation
    state_rules.validate_tick = validate_tick
