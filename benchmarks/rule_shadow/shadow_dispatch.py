"""Compare the Rust dispatch phase and the walk's kernel with their Python rules.

The loop goes through each rule's Python entry here (`use_python_entries`), so the other shadows see
every call and the kernels are the ones they replace. What only the loop decides is compared here:

- Each phase: the new record of each event of the due snapshot holds the bytes Python's json writes
  for that event, those records are in the queue's order, and no event of the snapshot stays
  pending. `id_counters["event"]` never goes back.
- The walk's `apply`: the Python rule on stand-ins of the person and the tasks, then the kernel on
  the
  world; the person's fields, the task's removal and what `_after_arrival` was asked. A rule
  that refuses must meet the kernel's Unsupported.

Plain runs of the gate or of 7 days say whether the direct calls ran: put SHELTERSIM_RULE_SHADOW and
SHELTERSIM_DISPATCH_COUNT_ONLY=1 in the environment, and this only writes `dispatch.stats()`.
"""

from __future__ import annotations

import atexit
import json
import os
from types import SimpleNamespace
from typing import Any

from shadow_kit import Recorder


def _person_copy(agent: Any) -> SimpleNamespace:
    return SimpleNamespace(
        id=agent.id,
        profile=agent.profile,
        zone_id=agent.zone_id,
        activity=agent.activity,
        target_id=agent.target_id,
        support_request_id=agent.support_request_id,
    )


_captured: list[tuple[Any, ...]] = []
_person_then: list[tuple[Any, ...]] = []


def _capture_after_arrival(handler_class: Any) -> None:
    """What the kernel asks of the handler, with no change to what the handler does."""
    original = handler_class._after_arrival
    if getattr(original, "captures", False):
        return

    def after_arrival(self: Any, world: Any, event: Any, *rest: Any) -> Any:
        _captured.append(rest)
        _person_then[:] = [_person(world.agents[rest[0]])]
        return original(self, world, event, *rest)

    after_arrival.captures = True  # type: ignore[attr-defined]
    handler_class._after_arrival = after_arrival


def _person(person: Any) -> tuple[Any, ...]:
    return person.zone_id, person.activity.value, person.target_id


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    recorder = Recorder(output, "dispatch")
    module = native.rules("dispatch")

    def report_counts() -> None:
        counts, reasons = module.stats()
        for key, count in counts.items():
            recorder.seen[f"loop.{key}"] += count
        for key, count in reasons.items():
            recorder.reasons[f"loop: {key}"] += count

    # After Recorder's own: atexit runs the last registered first.
    atexit.register(report_counts)
    if os.environ.get("SHELTERSIM_DISPATCH_COUNT_ONLY") == "1":
        return
    module.use_python_entries(True)
    unsupported = native.Unsupported
    run_phase = module.run_phase
    walks = native.rules("travel_completion")
    walk_kernel = walks.apply

    def phase(controller: Any, world: Any, minute: Any) -> Any:
        recorder.seen["phase.calls"] += 1
        before = len(world.event_log)
        counter = world.id_counters.get("event", 0)
        due = {event.event_id: event for event in world.event_queue.due(minute)}
        try:
            result = run_phase(controller, world, minute)
        except unsupported as reason:
            recorder.reasons[f"phase: {reason}"] += 1
            raise
        recorder.seen["phase.kernel_ran"] += 1
        recorder.seen["phase.due_events"] += len(due)
        _compare_records(recorder, world, minute, before, due)
        if world.id_counters.get("event", 0) < counter:
            recorder.differ(f"minute {minute}: the event counter went back")
        return result

    module.run_phase = phase

    def walk(handler: Any, world: Any, event: Any) -> Any:
        from sheltersim.events.handlers import AgentTravelCompletionHandler

        purpose = event.payload.get("purpose")
        recorder.seen[f"walk.calls.{purpose}"] += 1
        agent_id = event.payload.get("agent_id")
        task_id = event.payload.get("task_id")
        agents = dict(world.agents)
        if isinstance(agent_id, str) and agent_id in agents:
            agents[agent_id] = _person_copy(agents[agent_id])
        asked: list[tuple[Any, ...]] = []

        def after_arrival(_world: Any, _event: Any, *rest: Any) -> None:
            asked.append(rest)

        _capture_after_arrival(AgentTravelCompletionHandler)
        _captured.clear()
        _person_then.clear()

        stand_in = SimpleNamespace(
            active_tasks=dict(world.active_tasks),
            agents=agents,
            facilities=world.facilities,
            support_requests=world.support_requests,
            staff=world.staff,
            zones=world.zones,
            model_state=world.model_state,
            model_configs=world.model_configs,
            minute_index=world.minute_index,
            metric_state=dict(world.metric_state),
        )
        handler_copy = SimpleNamespace(
            resource_service=handler.resource_service,
            toilet_service=handler.toilet_service,
            validate_request_state=handler.validate_request_state,
            _after_arrival=after_arrival,
        )
        python_error: Exception | None = None
        python_ran = True
        if purpose in {"sleep_bed", "presence_relocation"}:
            # They write what they read: the Python rule runs once, after this says Unsupported.
            python_ran = False
        else:
            try:
                AgentTravelCompletionHandler.apply.__wrapped__(handler_copy, stand_in, event)
            except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
                python_error = raised
        try:
            result = walk_kernel(handler, world, event)
        except unsupported as reason:
            detail = f"{reason}" if python_error is None else f"Python raises: {reason}"
            recorder.reasons[f"walk: {purpose}: {detail}"] += 1
            if python_ran and python_error is None:
                recorder.differ(
                    f"walk {event.event_id}: Unsupported on what Python takes: {reason}"
                )
            raise
        recorder.seen[f"walk.kernel_ran.{purpose}"] += 1
        if not python_ran:
            return result
        if python_error is not None:
            recorder.differ(f"walk {event.event_id}: took what Python turned down: {python_error}")
            return result
        # What the walk leads to may send the person on, so as they were when it began.
        written = _person_then[0] if _person_then else _person(world.agents[agent_id])
        if _person(agents[agent_id]) != written:
            recorder.differ(f"walk {event.event_id}: {_person(agents[agent_id])} != {written}")
        elif (task_id in stand_in.active_tasks) != (task_id in world.active_tasks):
            recorder.differ(f"walk {event.event_id}: the task")
        else:
            recorder.seen["walk.values_compared"] += 4
        # The Python rule asks for every purpose, and for others the method does nothing.
        if purpose in {"toilet_request", "resource_request"}:
            if asked != _captured:
                recorder.differ(f"walk {event.event_id}: asked {_captured}, and Python {asked}")
            recorder.seen[f"walk.after_arrival.{purpose}"] += len(asked)
        return result

    walks.apply = walk


def _canonical(record: Any) -> bytes:
    return json.dumps(
        record.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _compare_records(
    recorder: Recorder, world: Any, minute: int, before: int, due: dict[str, Any]
) -> None:
    from sheltersim.events.models import EventLogRecord, EventStatus

    new = world.event_log.records_from(before)
    stored = world.event_log.items_bytes(before, before + len(new))
    for record, raw in zip(new, stored, strict=True):
        event = due.get(record.event.event_id)
        if event is None:
            continue
        recorder.seen["phase.records_compared"] += 1
        # The event's bytes from the object it was before the phase, by Python's encoder.
        written = _canonical(EventLogRecord(event, minute, record.status, record.detail))
        if written != raw:
            recorder.differ(f"minute {minute} {event.event_id}: {written!r} != {raw!r}")
        if (record.status is EventStatus.APPLIED) != (record.detail is None):
            recorder.differ(f"minute {minute} {event.event_id}: {record.status} {record.detail}")
    order = [record.event.event_id for record in new if record.event.event_id in due]
    if order != [event_id for event_id in due if event_id in set(order)]:
        recorder.differ(f"minute {minute}: records out of the queue's order: {order} {list(due)}")
    pending = {event.event_id for event in world.event_queue.pending()}
    for event_id in due:
        if event_id in pending and event_id not in set(order):
            recorder.differ(f"minute {minute}: {event_id} stayed pending")
