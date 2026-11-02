"""Compare native/src/rules/after_tick.rs with the Python check at each call.

The kernel answers yes or Unsupported, so at each yes the Python rules run too, without any memo,
and must pass. The memo it leaves is compared with what it must hold, worked out beside it from the
memo it started with. Every PROBE_EVERY-th yes also breaks a restored copy, one rule at a time, and
when the kernel takes a broken copy the Python rules must take it too, when this is the only
shadow: the others stand in for the kernels this one calls, and would report the break.
rules/metrics.rs's capture runs only in the driver, which shadows keep off: the hashes,
first_difference.py and tests/integration/test_metric_capture.py compare it.
"""

from __future__ import annotations

import sys
from typing import Any

from shadow_kit import Recorder

PROBE_EVERY = 997


def _is_settled(request: Any) -> bool:
    return request.status in ("resolved", "unresolved")


def _breaks(world: Any) -> list[tuple[str, Any]]:
    """(what is broken, how). Each probe gets its own copy."""
    state = world.model_state

    def set_in(holder: Any, name: str, value: Any) -> Any:
        return lambda: holder.__setitem__(name, value)

    found: list[tuple[str, Any]] = []
    if "resource_service" in world.model_configs:
        found.append(("a config", set_in(world.model_configs["resource_service"], "probe", 1)))
    agent = next(iter(world.agents.values()), None)
    if agent is not None:
        found.append(("stress 1% over", lambda: agent.__dict__.__setitem__("stress", 1.01)))
        zone = world.zones.get(agent.zone_id)
        inside = sum(other.zone_id == agent.zone_id for other in world.agents.values())
        if zone is not None and zone.capacity is not None:
            found.append(("a zone over", lambda: zone.__dict__.__setitem__("capacity", inside - 1)))
    facility = next(iter(world.facilities.values()), None)
    if facility is not None:
        found.append(("a facility moved", lambda: setattr(facility, "zone_id", "elsewhere")))
    body = state.get("body_state_v1")
    if body is not None:
        found.append(("body config", set_in(body["config"], "probe", 1)))
        found.append(
            (
                "body config names nobody",
                set_in(body["config"], "sleep_debt_stress_rate_by_agent", {"nobody": 0.001}),
            )
        )
        if body["history"]:
            found.append(("body actor", set_in(body["history"][-1], "actor_ids", ["nobody"])))
            found.append(("body minute", set_in(body["history"][-1], "minute", -1)))
    if "operational_events_v1" in state:
        found.append(("operational", set_in(state["operational_events_v1"], "schema_version", 2)))
    if "zone_environment_v1" in state:
        found.append(("environment", set_in(state["zone_environment_v1"], "modifiers", [])))
    presence = state.get("population_presence_v1")
    if presence is not None:
        found.append(("presence", set_in(presence, "probe", 1)))
        if agent is not None and agent.profile.id in presence["people"]:
            person = presence["people"][agent.profile.id]
            found.append(("presence path", set_in(person, "archive_path", ["nowhere"])))
            found.append(
                ("presence future", set_in(person, "since_minute", world.minute_index + 1))
            )
    if "population_lifecycle_v1" in state:
        found.append(("lifecycle", set_in(state["population_lifecycle_v1"], "probe", 1)))
    operations = state.get("staff_operations_v1")
    if operations is not None:
        for record in operations["staff"].values():
            if record["phase"] != "idle":
                found.append(
                    ("staff task due", set_in(record, "due_minute", record["due_minute"] + 1))
                )
                break
        receipts = (state.get("shared_operations_v1") or {}).get("history") or []
        recent = [
            item
            for item in receipts[-10:]
            if item.get("event_type") == "staff_operation_transition"
            and item.get("minute", -1) >= world.minute_index - 1
        ]
        for row in operations["history"]:
            if recent and row["task_id"] == recent[-1]["task_id"]:
                found.append(("staff completion", set_in(row, "reason", "another reason")))
                break
    return found


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    recorder = Recorder(output, "after_tick")
    unsupported = native.Unsupported
    rules = native.rules("after_tick")
    kernel = rules.after_tick
    accepted = [0]

    def run(world: Any, memo: Any) -> bool:
        from sheltersim.core.context import ContextScope
        from sheltersim.domain.geometry_overlay import _CONFIG_SCOPE

        try:
            with ContextScope(_CONFIG_SCOPE, memo.geometry_scope):
                kernel(world, memo, True)
        except unsupported:
            return False
        return True

    def python_error(world: Any) -> str | None:
        try:
            world.validate(validate_event_history=False)
        except Exception as error:  # noqa: BLE001 - only compared with what the kernel did
            return str(error) or type(error).__name__
        return None

    def fresh(world: Any) -> tuple[Any, Any] | None:
        from sheltersim.core.world import WorldState

        try:
            copy = WorldState.from_dict(world.to_dict())
        except (ValueError, TypeError, KeyError):
            return None
        memo = copy.__dict__["_tick_memo"] = rules.TickMemo()
        return copy, memo

    def probe(world: Any) -> None:
        start = fresh(world)
        if start is None or not run(*start):
            recorder.seen["probe_no_copy"] += 1
            return
        for what, _ in _breaks(start[0]):
            again = fresh(world)
            if again is None:
                continue
            copy, memo = again
            run(copy, memo)
            dict(_breaks(copy))[what]()
            recorder.seen["probes"] += 1
            if not run(copy, memo):
                recorder.seen["probes_refused"] += 1
                continue
            error = python_error(copy)
            if error is not None:
                recorder.differ(f"the kernel took {what}, which Python turns down: {error}")
            else:
                recorder.seen["probes_still_valid"] += 1

    def expected_after(world: Any, facts: dict[str, Any]) -> dict[str, Any]:
        requests = list(world.support_requests.values())
        held = facts["requests"]
        starts = len(requests) >= len(held) and all(a is b for a, b in zip(held, requests))
        if not starts or facts["agent_ids"] != sorted(world.agents):
            checked, reopened = requests, False
            recorder.seen["requests_read_again"] += 1
        else:
            checked = list({id(item): item for item in facts["open"] + facts["written"]}.values())
            known = {id(item) for item in checked}
            checked += [item for item in requests[len(held) :] if id(item) not in known]
            reopened = any(not _is_settled(item) for item in facts["written"])
        still_open = [item for item in checked if not _is_settled(item)]
        if reopened:
            still_open = [item for item in requests if not _is_settled(item)]
        return {"checked": checked, "open": still_open, "held": len(requests)}

    def shadow(world: Any, memo: Any, _from_shadow: bool = False) -> None:
        recorder.seen["calls"] += 1
        before = memo.facts()
        expected = expected_after(world, before)
        try:
            kernel(world, memo, True)
        except unsupported as reason:
            recorder.reasons[str(reason)] += 1
            raise
        recorder.seen["kernel_ran"] += 1
        if before["agent_ids"] is not None and before["agent_ids"] != sorted(world.agents):
            recorder.seen["people_changed_and_taken"] += 1
        # The Python rules on the same state, with nothing skipped.
        error = python_error(world)
        if error is not None:
            recorder.differ(f"the kernel took what Python turned down: {error}")
        after = memo.facts()
        written = after["written"]
        same = (
            [id(item) for item in after["open"]] == [id(item) for item in expected["open"]]
            and len(after["requests"]) == expected["held"]
            and written == []
            and after["minute"] == world.minute_index
            and all(
                item._write_listener is (written if _is_settled(item) else None)
                for item in expected["checked"]
            )
            and after["agent_ids"] == sorted(world.agents)
            and after["zone_ids"] == sorted(world.zones)
            and after["facility_zones"] == {k: v.zone_id for k, v in world.facilities.items()}
        )
        if not same:
            recorder.differ(f"the memo differs at minute {world.minute_index}")
        for name, fact in after["unchanged"].items():
            recorder.seen[f"unchanged.{name}.{'yes' if fact else 'no'}"] += 1
        recorder.seen["requests_compared"] += len(expected["checked"])
        recorder.seen["open_requests"] += len(expected["open"])
        recorder.seen["people_read"] += len(world.agents)
        operations = world.model_state.get("staff_operations_v1") or {}
        recorder.seen["staff_tasks_read"] += sum(
            record["phase"] != "idle" for record in operations.get("staff", {}).values()
        )
        accepted[0] += 1
        if accepted[0] % PROBE_EVERY == 0 and "shadow_metrics" not in sys.modules:
            probe(world)

    rules.after_tick = shadow
