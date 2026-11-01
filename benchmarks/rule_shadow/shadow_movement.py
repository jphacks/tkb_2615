"""Compare every kernel of native/src/rules/movement.rs with its Python rule at each call.

A writer's kernel runs on the world first, on a plain copy taken before it, and the Python rule then
runs on that copy with the world's other parts: it must give the same document and result. The
order is this way round because a queue trip's replan, which the kernel refuses, writes the queue:
the Python rule only runs on the copy after the kernel has taken the call, and its queue here can
only be read. Key order inside a record isn't compared, since a kernel leaves a record alone when
its bits are the same and a restored one has the JSON's order; the order of the maps is.

The validator is compared on what it accepts: the Python rule must accept that too. What it refuses
goes to the Python rule anyway.
"""

from __future__ import annotations

import json
from typing import Any

from shadow_kit import Recorder, values_in

KEY = "movement_v1"
ROW_CHECK_EVERY = 25
MAPS = ("positions", "staff_positions", "journeys", "edge_usage_minutes")


class _ReadOnlyQueue:
    def __init__(self, queue: Any) -> None:
        self.pending = queue.pending


class _Twin:
    """The world for a Python rule: a plain copy of the namespace and a queue it can't write."""

    def __init__(self, world: Any, plain: dict[str, Any]) -> None:
        table = dict(world.model_state)
        table[KEY] = plain
        self.__dict__.update(model_state=table, _world=world)
        self.__dict__["event_queue"] = _ReadOnlyQueue(world.event_queue)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_world"], name)


def _document(state: dict[str, Any]) -> str:
    return json.dumps(state, sort_keys=True, ensure_ascii=False)


def _order(state: dict[str, Any]) -> list[list[str]]:
    return [list(state[name]) for name in MAPS]


def _count_branches(recorder: Recorder, state: dict[str, Any], result: Any) -> None:
    seen = recorder.seen
    seen["advance.replans_returned"] += len(result)
    for journey in state["journeys"].values():
        seen["advance.journeys_in_rooms"] += bool(journey["route_points"])
        seen["advance.journeys_on_edges"] += bool(journey["route_edge_ids"])
        seen["advance.journeys_of_queues"] += "queue_id" in journey
        seen["advance.journeys_of_support"] += "support_request_id" in journey


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    rules = native.rules("movement")
    recorder = Recorder(output, "movement")
    unsupported = native.Unsupported
    module: list[Any] = []

    def domain() -> Any:
        if not module:
            from sheltersim.domain import movement

            module.append(movement)
        return module[0]

    def row_check(world: Any, label: str) -> None:
        recorder.seen["row_checks.due"] += 1
        if recorder.seen["row_checks.due"] % ROW_CHECK_EVERY:
            return
        mirror = world._core.mirror(KEY)
        if not mirror.verify():
            recorder.differ(f"{label}: the row and the tree")
        if mirror.row_json() != native.canonical_json(world.model_state[KEY]):
            recorder.differ(f"{label}: the row's bytes and the tree's")
        recorder.seen["row_checks.made"] += 1

    def writer(name: str, kernel: Any) -> Any:
        def run(world: Any, *arguments: Any, **options: Any) -> Any:
            if isinstance(world, _Twin):
                raise unsupported("a shadow's copy")
            recorder.seen[f"{name}.calls"] += 1
            state = world.model_state.get(KEY)
            plain = None if state is None else json.loads(json.dumps(state))
            try:
                result = kernel(world, *arguments, **options)
            except unsupported as reason:
                recorder.reasons[f"{name}: {reason}"] += 1
                raise
            recorder.seen[f"{name}.kernel_ran"] += 1
            if plain is None:
                return result
            where = f"{name} {arguments!r:.100} at {world.minute_index}"
            before = _document(plain)
            try:
                expected = getattr(domain(), name).__wrapped__(
                    _Twin(world, plain), *arguments, **options
                )
            except Exception as raised:  # noqa: BLE001 - only compared with what the kernel did
                recorder.differ(
                    f"{where}: the kernel took what Python turned down: {raised!r:.300}"
                )
                return result
            now = world.model_state[KEY]
            if result != expected:
                recorder.differ(f"{where}: returned {result!r:.200}, not {expected!r:.200}")
            mine, theirs = _document(now), _document(plain)
            if mine != theirs:
                at = next((i for i, pair in enumerate(zip(mine, theirs)) if pair[0] != pair[1]), 0)
                start, end = max(0, at - 150), at + 150
                recorder.differ(f"{where}: ...{mine[start:end]} is not ...{theirs[start:end]}")
            if _order(now) != _order(plain):
                recorder.differ(f"{where}: the order of a map")
            recorder.seen[f"{name}.values_compared"] += values_in(plain)
            recorder.seen[f"{name}.changed"] += before != theirs
            if name == "advance_movement":
                _count_branches(recorder, plain, result)
            row_check(world, where)
            return result

        return run

    for name in ("advance_movement", "start_movement", "finish_movement"):
        setattr(rules, name, writer(name, getattr(rules, name)))

    def reader(kernel_name: str, rule_name: str) -> Any:
        """A kernel that changes nothing: the repr of the rule's result, which shows float bits."""
        kernel = getattr(rules, kernel_name)

        def run(world: Any, *arguments: Any, **options: Any) -> Any:
            if isinstance(world, _Twin):
                raise unsupported("a shadow's copy")
            recorder.seen[f"{kernel_name}.calls"] += 1
            try:
                result = kernel(world, *arguments, **options)
            except unsupported as reason:
                recorder.reasons[f"{kernel_name}: {reason}"] += 1
                raise
            recorder.seen[f"{kernel_name}.kernel_ran"] += 1
            try:
                expected = getattr(domain(), rule_name).__wrapped__(world, *arguments, **options)
            except Exception as raised:  # noqa: BLE001 - only compared with what the kernel did
                recorder.differ(
                    f"{kernel_name}: the kernel took what Python turned down: {raised!r:.300}"
                )
                return result
            if repr(result) != repr(expected):
                recorder.differ(
                    f"{kernel_name} {arguments!r:.120} {options!r:.120}: "
                    f"{result!r:.300} is not {expected!r:.300}"
                )
            recorder.seen[f"{kernel_name}.values_compared"] += 1
            recorder.seen[f"{kernel_name}.none"] += result is None
            return result

        return run

    for kernel_name, rule_name in (
        ("route_with_movement_time", "route_with_movement_time"),
        ("route_is_physically_clear", "route_is_physically_clear"),
        ("plan_in_room_route", "plan_in_room_route"),
        ("queue_slot_position", "_queue_slot_position"),
        ("facility_approach_route", "facility_approach_route"),
    ):
        setattr(rules, kernel_name, reader(kernel_name, rule_name))

    validate_kernel = rules.validate

    def validate(world: Any) -> bool:
        recorder.seen["validate.calls"] += 1
        try:
            accepted = validate_kernel(world)
        except unsupported as reason:
            recorder.reasons[f"validate: {reason}"] += 1
            raise
        recorder.seen[f"validate.{'accepted' if accepted else 'refused'}"] += 1
        if accepted:
            try:
                domain().validate_movement_runtime_state(world)
            except Exception as raised:  # noqa: BLE001 - only compared with what the kernel did
                recorder.differ(
                    f"validate: the kernel took what Python turned down: {raised!r:.300}"
                )
        return accepted

    rules.validate = validate
