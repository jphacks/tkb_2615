"""Compare every kernel of native/src/rules/queues.rs with its Python rule at each call.

A writer's kernel runs on the world first, and the Python rule then runs on a copy made before it:
plain copies of the movement state, the metrics, the event counter, the facilities and the people,
and a list of what it schedules. Everything those hold must come out the same, floats by their bits,
along with the result and each event's repr. What the kernel refuses isn't compared: the Python rule
runs on the world then.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from shadow_kit import Recorder, values_in
from shadow_movement import _Twin

KEY = "movement_v1"
SERVICE_KEY = "resource_service_v1"
QUEUES_KEY = "facility_queues_v1"
ROW_CHECK_EVERY = 25
QUEUE_LISTS = (
    "queue_agent_ids",
    "active_agent_ids",
    "resource_queue_agent_ids",
    "resource_active_agent_ids",
)


class _NoLog:
    """A rule that writes the log has given someone up, which the kernel must have refused."""

    def append(self, record: Any) -> None:
        raise AssertionError(f"the Python rule wrote to the event log: {record!r:.200}")


class _QueueTwin(_Twin):
    def __init__(self, world: Any, plain: dict[str, Any] | None) -> None:
        super().__init__(world, plain)
        table = self.__dict__["model_state"]
        if plain is None:
            del table[KEY]
        service = world.model_state.get(SERVICE_KEY)
        if isinstance(service, dict):
            table[SERVICE_KEY] = {"stall_state": json.loads(json.dumps(service["stall_state"]))}
        queues = world.model_state.get(QUEUES_KEY)
        if queues is not None:
            table[QUEUES_KEY] = json.loads(json.dumps(queues))
        self.__dict__.update(
            metric_state=dict(world.metric_state),
            id_counters=dict(world.id_counters),
            active_tasks=dict(world.active_tasks),
            agents={key: SimpleNamespace(**vars(item)) for key, item in world.agents.items()},
            facilities={
                key: SimpleNamespace(
                    **{
                        **vars(item),
                        **{name: list(getattr(item, name)) for name in QUEUE_LISTS},
                    }
                )
                for key, item in world.facilities.items()
            },
            event_log=_NoLog(),
            scheduled=[],
        )

    def allocate_event_sequence(self) -> int:
        counters = self.__dict__["id_counters"]
        sequence = counters.get("event", 0)
        counters["event"] = sequence + 1
        return sequence

    def schedule_event(self, event: Any) -> None:
        self.__dict__["scheduled"].append(event)
        counters = self.__dict__["id_counters"]
        counters["event"] = max(counters.get("event", 0), event.sequence + 1)


def _plain_movement(world: Any) -> dict[str, Any] | None:
    state = world.model_state.get(KEY)
    return None if state is None else json.loads(json.dumps(state))


def _document(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _compare(recorder: Recorder, where: str, world: Any, twin: Any, pending_before: int) -> None:
    """What the kernel left in the world against what the Python rule left in its copy."""
    fields = twin.__dict__
    state = world.model_state.get(KEY)
    if state is not None:
        plain = fields["model_state"][KEY]
        if _document(state) != _document(plain):
            recorder.differ(f"{where}: movement {_document(state)[:300]}")
        for name in ("positions", "journeys"):
            if list(state[name]) != list(plain[name]):
                recorder.differ(f"{where}: the order of {name}")
        recorder.seen["values_compared"] += values_in(plain)
    if _document(world.metric_state) != _document(fields["metric_state"]):
        recorder.differ(f"{where}: the metrics")
    if world.id_counters != fields["id_counters"]:
        recorder.differ(f"{where}: {world.id_counters} not {fields['id_counters']}")
    for key, facility in world.facilities.items():
        theirs = fields["facilities"][key]
        for name in QUEUE_LISTS:
            if list(getattr(facility, name)) != getattr(theirs, name):
                recorder.differ(f"{where}: {key}.{name}")
    for key, agent in world.agents.items():
        theirs = fields["agents"][key]
        if agent.activity is not theirs.activity or agent.target_id != theirs.target_id:
            recorder.differ(f"{where}: {key}'s activity or target")
    service = world.model_state.get(SERVICE_KEY)
    if isinstance(service, dict):
        mine = _document(service["stall_state"])
        if mine != _document(fields["model_state"][SERVICE_KEY]["stall_state"]):
            recorder.differ(f"{where}: the stall records")
    pending = {event.event_id: event for event in world.event_queue.pending()}
    if len(pending) != pending_before + len(fields["scheduled"]):
        recorder.differ(f"{where}: {len(pending)} pending, {pending_before} before")
    for event in fields["scheduled"]:
        recorder.seen["events_compared"] += 1
        real = pending.get(event.event_id)
        if real is None or repr(real) != repr(event):
            recorder.differ(f"{where}: event {real!r:.300} is not {event!r:.300}")
        else:
            phase = event.provenance["queue_phase"]
            recorder.seen[f"trips.{phase}"] += 1


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    rules = native.rules("queues")
    recorder = Recorder(output, "queues")
    unsupported = native.Unsupported

    def row_check(world: Any, where: str) -> None:
        recorder.seen["row_checks.due"] += 1
        if recorder.seen["row_checks.due"] % ROW_CHECK_EVERY or KEY not in world.model_state:
            return
        mirror = world._core.mirror(KEY)
        if not mirror.verify():
            recorder.differ(f"{where}: the row and the tree")
        if mirror.row_json() != native.canonical_json(world.model_state[KEY]):
            recorder.differ(f"{where}: the row's bytes and the tree's")
        recorder.seen["row_checks.made"] += 1

    def rule_of(module: str, name: str) -> Any:
        def get() -> Any:
            import importlib

            return getattr(importlib.import_module(f"sheltersim.domain.{module}"), name).__wrapped__

        return get

    def writer(name: str, module: str, kernel: Any) -> Any:
        def run(world: Any, *arguments: Any, **options: Any) -> Any:
            if isinstance(world, _Twin):
                raise unsupported("a shadow's copy")
            recorder.seen[f"{name}.calls"] += 1
            facility = world.facilities.get(arguments[0])
            if name == "prepare_facility_queue" and (
                facility is None
                or not (
                    facility.resource_queue_agent_ids
                    if options.get("resource")
                    else facility.queue_agent_ids
                )
            ):
                # The Python rule returns at its first lines.
                recorder.seen[f"{name}.empty_queue"] += 1
                result = kernel(world, *arguments, **options)
                recorder.seen[f"{name}.kernel_ran"] += 1
                return result
            twin = _QueueTwin(world, _plain_movement(world))
            metric_before = _document(world.metric_state)
            pending_before = len(world.event_queue.pending())
            order_before = [
                list(getattr(item, entry))
                for item in world.facilities.values()
                for entry in QUEUE_LISTS
            ]
            service = world.model_state.get(SERVICE_KEY)
            stalls_before = len(service["stall_state"]) if isinstance(service, dict) else 0
            try:
                result = kernel(world, *arguments, **options)
            except unsupported as reason:
                recorder.reasons[f"{name}: {reason}"] += 1
                raise
            recorder.seen[f"{name}.kernel_ran"] += 1
            where = f"{name} {arguments!r:.100} {options!r:.100} at {world.minute_index}"
            try:
                expected = rule_of(module, name)()(twin, *arguments, **options)
            except Exception as raised:  # noqa: BLE001 - only compared with what the kernel did
                recorder.differ(
                    f"{where}: the kernel took what Python turned down: {raised!r:.300}"
                )
                return result
            if result != expected:
                recorder.differ(f"{where}: returned {result!r:.100}, not {expected!r:.100}")
            _compare(recorder, where, world, twin, pending_before)
            recorder.seen[f"{name}.returned_{result}"] += 1
            if _document(world.metric_state) != metric_before:
                recorder.seen[f"{name}.blocked"] += 1
            order_now = [
                list(getattr(item, entry))
                for item in world.facilities.values()
                for entry in QUEUE_LISTS
            ]
            if order_now != order_before:
                recorder.seen[f"{name}.reordered"] += 1
            if isinstance(service, dict) and len(service["stall_state"]) != stalls_before:
                recorder.seen[f"{name}.stalls_dropped"] += 1
            row_check(world, where)
            return result

        return run

    for name, module in (
        ("prepare_facility_queue", "queueing"),
        ("progress_physical_queue_positions", "movement"),
        ("progress_physical_queue_head_to_service", "movement"),
    ):
        setattr(rules, name, writer(name, module, getattr(rules, name)))
