"""Compare native/src/rules/after_tick.rs with the Python check at each call.

The kernel answers yes or Unsupported, so at each yes the Python rules run too, without any memo,
and must pass. The memo it leaves is compared with what `commit` would leave, worked out beside it
from the same start. Every PROBE_EVERY-th yes also breaks a restored copy, one rule at a time, and
the kernel must turn each down, when this is the only shadow: the others stand in for the kernels
this one calls, and would report the break. rules/metrics.rs's capture runs only in the driver,
which shadows keep off: the hashes, first_difference.py and tests/integration/test_metric_capture.py
compare it.
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

    found: list[tuple[str, Any]] = [("a config written", set_in(world.model_configs, "probe", {}))]
    agent = next(iter(world.agents.values()), None)
    if agent is not None:
        found.append(("stress 1% over", lambda: agent.__dict__.__setitem__("stress", 1.01)))
    facility = next(iter(world.facilities.values()), None)
    if facility is not None:
        found.append(("a facility moved", lambda: setattr(facility, "zone_id", "elsewhere")))
    body = state.get("body_state_v1")
    if body is not None:
        found.append(("body config", set_in(body["config"], "probe", 1)))
        if body["history"]:
            found.append(("body actor", set_in(body["history"][-1], "actor_ids", ["nobody"])))
            found.append(("body minute", set_in(body["history"][-1], "minute", -1)))
    if "operational_events_v1" in state:
        found.append(("operational", set_in(state["operational_events_v1"], "schema_version", 2)))
    if "zone_environment_v1" in state:
        found.append(("environment", set_in(state["zone_environment_v1"], "modifiers", [])))
    if "population_presence_v1" in state:
        found.append(("presence", set_in(state["population_presence_v1"], "probe", 1)))
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

    def primed_copy(world: Any) -> tuple[Any, Any] | None:
        from sheltersim.core.tick_validation import TickValidationMemo
        from sheltersim.core.world import WorldState

        try:
            copy = WorldState.from_dict(world.to_dict())
            memo = copy.__dict__["_tick_memo"] = TickValidationMemo()
            with memo.config_scope():
                copy._validate_with_memo(memo)
        except (ValueError, TypeError, KeyError):
            return None
        return copy, memo

    def probe(world: Any) -> None:
        primed = primed_copy(world)
        if primed is None:
            recorder.seen["probe_no_copy"] += 1
            return
        for what, _ in _breaks(primed[0]):
            again = primed_copy(world)
            if again is None:
                continue
            copy, memo = again
            how = dict(_breaks(copy))[what]
            how()
            recorder.seen["probes"] += 1
            try:
                with memo.config_scope():
                    kernel(copy, memo, True)
                recorder.differ(f"the kernel took {what}")
            except unsupported:
                pass

    def shadow(world: Any, memo: Any, _from_shadow: bool = False) -> Any:
        recorder.seen["calls"] += 1
        # What the Python check would leave, from the memo it would start with.
        requests = world.support_requests
        added = memo._requests.added_values(requests)
        checked: dict[int, Any] = {}
        if added is not None:
            checked = {id(item): item for item in memo._open_requests}
            checked.update((id(item), item) for item in memo._written_requests)
            checked.update((id(item), item) for item in added)
        reopened = any(not _is_settled(item) for item in memo._written_requests)
        held = len(memo._requests) + (0 if added is None else len(added))
        try:
            left = kernel(world, memo, True)
        except unsupported as reason:
            recorder.reasons[str(reason)] += 1
            raise
        recorder.seen["kernel_ran"] += 1
        if left:
            recorder.seen["zones_left_to_python"] += len(left)
        # The Python rules on the same state, with nothing skipped.
        try:
            world.validate(validate_event_history=False)
            for zone_id, occupancy in left:
                world._validate_zone_capacity(zone_id, occupancy)
        except Exception as error:  # noqa: BLE001 - only compared with what the kernel did
            recorder.differ(f"the kernel took what Python turned down: {error}")
        still_open = [item for item in checked.values() if not _is_settled(item)]
        if reopened:
            still_open = [item for item in requests.values() if not _is_settled(item)]
        same = (
            added is not None
            and [id(item) for item in memo._open_requests] == [id(item) for item in still_open]
            and len(memo._requests) == held
            and memo._written_requests == []
            and memo.minute == world.minute_index
            and all(
                item._write_listener is (memo._written_requests if _is_settled(item) else None)
                for item in checked.values()
            )
            and world.agents.keys() == memo._agent_ids
            and world.zones.keys() == memo._zone_ids
            and {key: item.zone_id for key, item in world.facilities.items()}
            == memo._facility_zones
        )
        if not same:
            recorder.differ(f"the memo differs at minute {world.minute_index}")
        recorder.seen["requests_compared"] += len(checked)
        recorder.seen["open_requests"] += len(still_open)
        recorder.seen["people_read"] += len(world.agents)
        accepted[0] += 1
        if accepted[0] % PROBE_EVERY == 0 and "shadow_metrics" not in sys.modules:
            probe(world)
        return left

    rules.after_tick = shadow
