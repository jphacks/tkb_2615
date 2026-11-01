"""Compare every kernel of rules/service.rs and rules/travel.rs with its Python rule at each call.

The Python rule runs first on a copy of everything it writes: plain copies of the two namespaces
(the histories share their old records), the people, the facilities, the staff, the tasks, the
metrics, the counters and the movement state, with lists of what it schedules and logs. The kernel
then runs on the world. Everything those hold must come out the same, floats by their bits, along
with the result and each event's and record's repr. What the kernel refuses isn't compared: the
Python rule runs on the world then. Whatever the validator accepts must pass the Python rule.
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any

from shadow_kit import Recorder, text, values_in
from shadow_movement import _Twin

MOVEMENT = "movement_v1"
LEDGER = "resource_ledger_v1"
SERVICE = "resource_service_v1"
QUEUES = "facility_queues_v1"
OVERLAY = "geometry_overlay_v1"
HISTORIES = {"request_history", "queue_stall_history"}
ROW_CHECK_EVERY = 25
QUEUE_LISTS = (
    "queue_agent_ids",
    "active_agent_ids",
    "resource_queue_agent_ids",
    "resource_active_agent_ids",
)
# Every kernel a Python rule below can reach: on a copy, the rule itself has to run.
GUARDED = (
    "movement",
    "queues",
    "resources",
    "staff_dynamics",
    "agent_dynamics",
    "service",
    "travel",
    "common_operations",
)


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value))


class _Log:
    def __init__(self, log: Any) -> None:
        self.contains_id = log.contains_id
        self.contains_sequence = log.contains_sequence
        self.appended: list[Any] = []

    def append(self, record: Any) -> None:
        self.appended.append(record)


class _ServiceTwin(_Twin):
    def __init__(self, world: Any) -> None:
        movement = world.model_state.get(MOVEMENT)
        super().__init__(world, None if movement is None else _plain(movement))
        table = self.__dict__["model_state"]
        if movement is None:
            del table[MOVEMENT]
        ledger = world.model_state.get(LEDGER)
        if ledger is not None:
            table[LEDGER] = {
                **{key: _plain(value) for key, value in ledger.items() if key != "audit"},
                "audit": list(ledger["audit"]),
            }
        service = world.model_state.get(SERVICE)
        if service is not None:
            table[SERVICE] = {
                key: list(value) if key in HISTORIES else _plain(value)
                for key, value in service.items()
            }
        # A Python rule the kernel refuses writes these too, and the copy must keep it from them.
        queues = world.model_state.get(QUEUES)
        if queues is not None:
            table[QUEUES] = _plain(queues)
        overlay = world.model_state.get(OVERLAY)
        if isinstance(overlay, dict) and isinstance(overlay.get("bed_reservations"), dict):
            table[OVERLAY] = {**overlay, "bed_reservations": dict(overlay["bed_reservations"])}
        self.__dict__.update(
            metric_state=dict(world.metric_state),
            id_counters=dict(world.id_counters),
            resources=dict(world.resources),
            active_tasks=dict(world.active_tasks),
            agents={
                key: SimpleNamespace(
                    **{**vars(item), "needs": SimpleNamespace(**vars(item.needs))}, id=item.id
                )
                for key, item in world.agents.items()
            },
            facilities={
                key: SimpleNamespace(
                    **{**vars(item), **{name: list(getattr(item, name)) for name in QUEUE_LISTS}}
                )
                for key, item in world.facilities.items()
            },
            staff={
                key: SimpleNamespace(**{**vars(item), "work_queue": list(item.work_queue)})
                for key, item in world.staff.items()
            },
            event_log=_Log(world.event_log),
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


def _document(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class _NoService:
    """In place of a manager of the world: the kernels refuse what reaches it."""

    def request(self, *_arguments: Any, **_options: Any) -> None:
        raise RuntimeError("a Python rule reached a service of the world")


def manager_stand_in(manager: Any, twin: Any) -> Any:
    """The manager on the copy: the rule's own methods, with the copy as its world."""
    stand_in = type(manager).__new__(type(manager))
    stand_in.world = twin
    stand_in.config = manager.config
    return stand_in


def controller_stand_in(controller: Any, twin: Any) -> Any:
    """The controller on the copy, with the Python steps: its base class has no kernels."""
    from sheltersim.experiments.phases.propose import ProposePhase

    class StandIn(ProposePhase):
        pass

    stand_in = StandIn()
    stand_in.__dict__.update(controller.__dict__)
    stand_in.world = twin
    if controller.resource_service is not None:
        stand_in.resource_service = manager_stand_in(controller.resource_service, twin)
    # These hold the world: a rule that reaches them must not write it from here.
    stand_in.toilet_service = _NoService()
    stand_in.staff_service = _NoService()
    return stand_in


def compare_after(
    recorder: Recorder, where: str, world: Any, twin: Any, pending_before: int
) -> None:
    """What the kernel left in the world against what the Python rule left in its copy."""
    _compare(recorder, where, world, twin, pending_before)


def _compare(recorder: Recorder, where: str, world: Any, twin: Any, pending_before: int) -> None:
    fields = twin.__dict__
    seen = recorder.seen
    table = fields["model_state"]
    for name in (LEDGER, SERVICE):
        if name not in table:
            continue
        theirs = table[name]
        mine = world.model_state[name]
        if name == LEDGER:
            # The old records are the same objects: the new ones are what can differ.
            old = len(theirs["audit"])
            if len(mine["audit"]) != old or text(mine["audit"][-4:]) != text(theirs["audit"][-4:]):
                recorder.differ(f"{where}: the audit trail {text(mine['audit'][-1:]):.300}")
            mine = {key: value for key, value in mine.items() if key != "audit"}
            theirs = {key: value for key, value in theirs.items() if key != "audit"}
        else:
            for history in HISTORIES:
                count = len(theirs[history])
                if len(mine[history]) != count or text(mine[history][-4:]) != text(
                    theirs[history][-4:]
                ):
                    recorder.differ(f"{where}: {history} {text(mine[history][-1:]):.300}")
                seen["history_records"] += min(count, 4)
            mine = {key: value for key, value in mine.items() if key not in HISTORIES}
            theirs = {key: value for key, value in theirs.items() if key not in HISTORIES}
        if text(mine) != text(theirs):
            recorder.differ(f"{where}: {name} {text(mine):.300} not {text(theirs):.300}")
        seen["values_compared"] += values_in(theirs)
    overlay = world.model_state.get(OVERLAY)
    if isinstance(overlay, dict) and text(overlay.get("bed_reservations")) != text(
        table[OVERLAY].get("bed_reservations")
    ):
        recorder.differ(f"{where}: the bed reservations")
    movement = world.model_state.get(MOVEMENT)
    if movement is not None:
        if _document(movement) != _document(table[MOVEMENT]):
            recorder.differ(f"{where}: the movement state")
        for name in ("positions", "journeys"):
            if list(movement[name]) != list(table[MOVEMENT][name]):
                recorder.differ(f"{where}: the order of {name}")
    if _document(world.metric_state) != _document(fields["metric_state"]):
        recorder.differ(f"{where}: the metrics")
    if world.id_counters != fields["id_counters"]:
        recorder.differ(f"{where}: {world.id_counters} not {fields['id_counters']}")
    if text(world.resources) != text(fields["resources"]):
        recorder.differ(f"{where}: the resources")
    for key, facility in world.facilities.items():
        theirs = fields["facilities"][key]
        for name in QUEUE_LISTS:
            if list(getattr(facility, name)) != getattr(theirs, name):
                recorder.differ(f"{where}: {key}.{name}")
    for key, agent in world.agents.items():
        theirs = fields["agents"][key]
        if (
            agent.activity is not theirs.activity
            or agent.target_id != theirs.target_id
            or text(vars(agent.needs)) != text(vars(theirs.needs))
        ):
            recorder.differ(f"{where}: {key}")
    for key, person in world.staff.items():
        theirs = fields["staff"][key]
        if (
            list(person.work_queue) != theirs.work_queue
            or person.active_task_id != theirs.active_task_id
            or person.busy_until != theirs.busy_until
        ):
            recorder.differ(f"{where}: staff {key}")
    if list(world.active_tasks) != list(fields["active_tasks"]):
        recorder.differ(f"{where}: the order of the tasks")
    for key, task in world.active_tasks.items():
        theirs = fields["active_tasks"].get(key)
        if theirs is None or task.to_dict() != theirs.to_dict():
            recorder.differ(f"{where}: task {key}")
    pending = {event.event_id: event for event in world.event_queue.pending()}
    if len(pending) != pending_before + len(fields["scheduled"]):
        recorder.differ(f"{where}: {len(pending)} pending, {pending_before} before")
    for event in fields["scheduled"]:
        seen["events_compared"] += 1
        real = pending.get(event.event_id)
        if real is None or repr(real) != repr(event):
            recorder.differ(f"{where}: event {real!r:.300} is not {event!r:.300}")
    for record in fields["event_log"].appended:
        seen["log_records_compared"] += 1
        real = world.event_log.find(record.event.event_id)
        # A record read back from the log's bytes has its keys sorted.
        if real is None or _document(real.to_dict()) != _document(record.to_dict()):
            recorder.differ(f"{where}: record {real!r:.300} is not {record!r:.300}")


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    unsupported = native.Unsupported
    recorder = Recorder(output, "service")

    def on_copy(first: Any) -> bool:
        return isinstance(first, _Twin) or isinstance(getattr(first, "world", None), _Twin)

    def guarded(kernel: Any) -> Any:
        def run(first: Any, *arguments: Any, **options: Any) -> Any:
            if on_copy(first):
                raise unsupported("a shadow's copy")
            return kernel(first, *arguments, **options)

        return run

    for module in GUARDED:
        rules = native.rules(module)
        for name in dir(rules):
            kernel = getattr(rules, name)
            if callable(kernel) and not name.startswith("_") and not isinstance(kernel, type):
                setattr(rules, name, guarded(kernel))

    def manager_class() -> Any:
        return importlib.import_module("sheltersim.domain.resource_service").ResourceServiceManager

    def row_check(world: Any, where: str) -> None:
        recorder.seen["row_checks.due"] += 1
        if recorder.seen["row_checks.due"] % ROW_CHECK_EVERY:
            return
        if SERVICE in world.model_state:
            mirror = world._core.mirror(SERVICE)
            if not mirror.verify():
                recorder.differ(f"{where}: the row and the tree")
            if mirror.row_json() != native.canonical_json(world.model_state[SERVICE]):
                recorder.differ(f"{where}: the row's bytes and the tree's")
        problem = world._core.entities().verify(world)
        if problem:
            recorder.differ(f"{where}: the rows and the objects: {problem}")
        recorder.seen["row_checks.made"] += 1

    def writer(name: str, method: str, count: Any = None) -> None:
        rules = native.rules("service")
        kernel = getattr(rules, name)

        def run(manager: Any, *arguments: Any, **options: Any) -> Any:
            if on_copy(manager):
                raise unsupported("a shadow's copy")
            recorder.seen[f"{name}.calls"] += 1
            world = manager.world
            twin = _ServiceTwin(world)
            pending_before = len(world.event_queue.pending())
            stand_in = manager_stand_in(manager, twin)
            error = None
            expected = None
            try:
                expected = getattr(manager_class(), method).__wrapped__(
                    stand_in, *arguments, **options
                )
            except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
                error = raised
            try:
                result = kernel(manager, *arguments, **options)
            except unsupported as reason:
                recorder.reasons[f"{name}: {reason}"] += 1
                recorder.seen[f"{name}.unsupported_python_{'raised' if error else 'accepted'}"] += 1
                raise
            recorder.seen[f"{name}.kernel_ran"] += 1
            where = f"{name} {arguments!r:.120} {options!r:.100} at {world.minute_index}"
            if error is not None:
                recorder.differ(f"{where}: the kernel took what Python turned down: {error!r:.300}")
                return result
            if repr(result) != repr(expected):
                recorder.differ(f"{where}: returned {result!r:.200}, not {expected!r:.200}")
            _compare(recorder, where, world, twin, pending_before)
            if count is not None:
                count(result, arguments, options)
            row_check(world, where)
            return result

        setattr(rules, name, run)

    def count_head(result: Any, _arguments: Any, _options: Any) -> None:
        recorder.seen[f"serve_head.{'started' if result else 'stopped'}"] += 1

    writer("serve_head", "_serve_head", count_head)
    writer("complete", "complete")
    writer("request", "request")
    writer(
        "record_request_outcome",
        "record_request_outcome",
        lambda _result, _arguments, options: recorder.seen.update(
            {f"record_request_outcome.{options['outcome']}": 1}
        ),
    )

    # Stands beside the rule's list: what it leaves out, an empty queue, does nothing in Python.
    rules = native.rules("service")
    list_kernel = rules.facilities_to_serve

    def facilities_to_serve(manager: Any) -> Any:
        recorder.seen["facilities_to_serve.calls"] += 1
        try:
            result = list_kernel(manager)
        except unsupported as reason:
            recorder.reasons[f"facilities_to_serve: {reason}"] += 1
            raise
        recorder.seen["facilities_to_serve.kernel_ran"] += 1
        expected = [
            item
            for item in getattr(manager_class(), "_facilities_to_serve").__wrapped__(manager)
            if item[0].resource_queue_agent_ids
        ]
        if result != expected:
            recorder.differ(f"facilities_to_serve: {result!r:.200} not {expected!r:.200}")
        recorder.seen["facilities_to_serve.returned"] += len(result)
        return result

    rules.facilities_to_serve = facilities_to_serve

    travel = native.rules("travel")
    schedule_kernel = travel.schedule_travel

    def schedule_travel(controller: Any, **options: Any) -> Any:
        if on_copy(controller):
            raise unsupported("a shadow's copy")
        recorder.seen["schedule_travel.calls"] += 1
        world = controller.world
        twin = _ServiceTwin(world)
        pending_before = len(world.event_queue.pending())
        stand_in = controller_stand_in(controller, twin)
        error = None
        expected = None
        try:
            expected = stand_in._schedule_travel(**options)
        except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
            error = raised
        try:
            result = schedule_kernel(controller, **options)
        except unsupported as reason:
            recorder.reasons[f"schedule_travel: {reason}"] += 1
            raise
        recorder.seen["schedule_travel.kernel_ran"] += 1
        recorder.seen[f"schedule_travel.{options['purpose']}.{result}"] += 1
        where = f"schedule_travel {options!r:.200} at {world.minute_index}"
        if error is not None:
            recorder.differ(f"{where}: the kernel took what Python turned down: {error!r:.300}")
            return result
        if result != expected:
            recorder.differ(f"{where}: returned {result!r}, not {expected!r}")
        _compare(recorder, where, world, twin, pending_before)
        row_check(world, where)
        return result

    travel.schedule_travel = schedule_travel
    route_kernel = travel.route

    def route(controller: Any, *arguments: Any) -> Any:
        if on_copy(controller):
            raise unsupported("a shadow's copy")
        recorder.seen["route.calls"] += 1
        # Reads only, without edge capacities, which the kernel refuses.
        stand_in = controller_stand_in(controller, controller.world)
        try:
            result = route_kernel(controller, *arguments)
        except unsupported as reason:
            recorder.reasons[f"route: {reason}"] += 1
            raise
        recorder.seen["route.kernel_ran"] += 1
        expected = stand_in._route(*arguments)
        recorder.seen[f"route.{'none' if expected is None else 'found'}"] += 1
        if repr(result) != repr(expected):
            recorder.differ(f"route {arguments!r}: {result!r:.300} not {expected!r:.300}")
        recorder.seen["route.values_compared"] += 5
        return result

    travel.route = route

    state = native.state("resource_service")
    validate_kernel = state.validate

    def validate(world: Any) -> bool:
        accepted = validate_kernel(world)
        recorder.seen[f"validate.{'accepted' if accepted else 'refused'}"] += 1
        if accepted and recorder.seen["validate.accepted"] % ROW_CHECK_EVERY == 0:
            # The world's rule, reaching the namespace's Python rules with the kernel out.
            recorder.seen["validate.compared"] += 1
            from sheltersim.core import state_mirror

            kernel = state_mirror._RUST_VALIDATORS.pop(SERVICE, None)
            try:
                world.validate(validate_event_history=False)
            except ValueError as raised:
                recorder.differ(f"validate at {world.minute_index}: Python refused: {raised}")
            finally:
                if kernel is not None:
                    state_mirror._RUST_VALIDATORS[SERVICE] = kernel
        return accepted

    state.validate = validate
