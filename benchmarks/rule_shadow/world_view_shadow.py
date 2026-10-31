"""Compare the views of rules/world_view.rs with Python's at every call: see sitecustomize.py.

Each view is made both ways, and every field of it compared bit by bit: its type, a float's hex,
the order of each dict's keys, the audit hash. Python's own chain is kept apart: its update-phase
view comes from `from_world`, and its proposal view from `after_state_update` on that. A proposal
view is also compared with a new `from_world`. Python's caches are not used, so what Rust keeps
between ticks has to be what a new read says. Results are in DIR/world_view.
"""

from __future__ import annotations

import atexit
import json
import os
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def exact(value: Any, seen: Counter[str]) -> Any:
    """A form of the value in which two values are equal only with the same bits and types."""
    if value is None or type(value) is bool:
        return value
    if type(value) is float:
        seen["view_values_compared"] += 1
        return f"f{value.hex()}"
    if type(value) is int or isinstance(value, str):
        seen["view_values_compared"] += 1
        return [type(value).__name__, str(value) if isinstance(value, str) else value]
    if isinstance(value, tuple):
        return ["tuple", *(exact(item, seen) for item in value)]
    if isinstance(value, Mapping):
        return ["map", *([key, exact(item, seen)] for key, item in value.items())]
    if hasattr(value, "__dataclass_fields__"):
        return [
            type(value).__name__,
            *([key, exact(item, seen)] for key, item in value.__dict__.items()),
        ]
    raise TypeError(f"a view holds a {type(value).__name__}")


def describe(view: Any, seen: Counter[str]) -> str:
    return json.dumps(
        {
            "minute_index": exact(view.minute_index, seen),
            "state_hash": exact(view.state_hash, seen),
            "resource_service_enabled": exact(view.resource_service_enabled, seen),
            "autonomy_enabled": exact(view.autonomy_enabled, seen),
            "has_population_context": exact(view._has_population_context, seen),
            "agents": exact(view.agents, seen),
            "facilities": exact(view.facilities, seen),
            "zone_members": exact(view.zone_members, seen),
            "group_members": exact(view.group_members, seen),
            "immediate_needs": exact(view.immediate_needs(), seen),
        }
    )


def first_difference(python: str, rust: str) -> str:
    place = next(
        (i for i, (a, b) in enumerate(zip(python, rust, strict=False)) if a != b),
        min(len(python), len(rust)),
    )
    start, end = max(0, place - 120), place + 80
    return f"at {place}: python {python[start:end]!r} rust {rust[start:end]!r}"


def reached(view: Any, seen: Counter[str]) -> None:
    """Which branches the comparison reached, since a branch no scenario takes is not compared."""
    for agent in view.agents.values():
        seen["view_people"] += 1
        seen["view_people_in_a_group"] += agent.group_id is not None
        seen["view_people_with_context"] += agent.personal_context_present
        seen["view_people_queued"] += agent.queue_task_id is not None
        seen["view_people_with_request"] += agent.support_request_status is not None
        seen["view_people_with_query_pressure"] += agent.information_query_pressure != 0.0
        seen["view_people_misinformed"] += bool(agent.misinformation_effect_sources)
        seen["view_people_with_body_values"] += agent.sleep_debt_minutes != 0.0
        seen["view_people_with_chronic_distress"] += agent.chronic_distress != 0.0
        seen["view_people_with_demand_modifiers"] += bool(agent.demand_modifiers)
        seen["view_people_under_stressors"] += bool(agent.environmental_stressor_event_ids)
        seen["view_people_with_conditions"] += bool(agent.condition_modifier_event_ids)
        seen["view_people_with_noise"] += bool(agent.zone_environment_modifier_event_ids)
        seen["view_people_at_low_sanitation"] += agent.sanitation_level != 1.0
    for facility in view.facilities.values():
        seen["view_facilities"] += 1
        seen["view_facilities_with_context"] += bool(facility.facility_event_context_ids)


def install(output: str) -> None:
    from sheltersim import native
    from sheltersim.experiments.phases.propose import ProposePhase
    from sheltersim.experiments.phases.update import UpdatePhase
    from sheltersim.experiments.runner import OfflineSimulationController
    from sheltersim.policies.world_view import WorldView

    if not native.available():
        return
    seen: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    differences: list[str] = []
    rules = native.rules("world_view")
    after_movement_python = UpdatePhase._view_after_movement
    proposals_python = ProposePhase._view_for_proposals

    def after_movement(self: Any, world: Any) -> Any:
        seen["view_after_movement_calls"] += 1
        python = python_error = None
        try:
            python = WorldView.from_world(world, include_state_hash=False)
        except Exception as error:  # noqa: BLE001 - only compared with what the kernel does
            python_error = error
        self._shadow_view_chain = python
        try:
            made = rules.view_after_movement(self, world)
        except native.Unsupported as reason:
            reasons[f"view after movement: {reason}"] += 1
            return after_movement_python(self, world)
        seen["view_after_movement_ran"] += 1
        if python_error is not None:
            differences.append(f"minute {world.minute_index}: the view took what Python raises on")
            return made
        left, right = describe(python, seen), describe(made, seen)
        if left != right:
            differences.append(
                f"minute {world.minute_index}, update: {first_difference(left, right)}"
            )
        reached(made, seen)
        return made

    def for_proposals(self: Any, world: Any, minute: int) -> Any:
        seen["view_for_proposals_calls"] += 1
        chain = getattr(self, "_shadow_view_chain", None)
        prior = self._view_before_proposals
        python = python_error = None
        try:
            python = (
                WorldView.after_state_update(chain, world)
                if prior is not None and prior.minute_index == minute and chain is not None
                else WorldView.from_world(world)
            )
        except Exception as error:  # noqa: BLE001 - only compared with what the kernel does
            python_error = error
        self._shadow_view_chain = None
        try:
            made = rules.view_for_proposals(self, world, minute)
        except native.Unsupported as reason:
            reasons[f"view for proposals: {reason}"] += 1
            return proposals_python(self, world, minute)
        seen["view_for_proposals_ran"] += 1
        seen["view_refreshed"] += prior is not None and prior.minute_index == minute
        if python_error is not None:
            differences.append(f"minute {minute}: the view took what Python raises on")
            return made
        rust_text = describe(made, seen)
        left = describe(python, seen)
        if left != rust_text:
            differences.append(f"minute {minute}, propose: {first_difference(left, rust_text)}")
        fresh = describe(WorldView.from_world(world), seen)
        if fresh != rust_text:
            differences.append(f"minute {minute}, a new read: {first_difference(fresh, rust_text)}")
        reached(made, seen)
        return made

    OfflineSimulationController._view_after_movement = after_movement  # type: ignore[method-assign]
    OfflineSimulationController._view_for_proposals = for_proposals  # type: ignore[method-assign]

    target = Path(output) / "world_view"
    target.mkdir(parents=True, exist_ok=True)

    def report() -> None:
        if seen or reasons or differences:
            document = {"seen": seen, "unsupported": reasons, "differences": differences[:20]}
            (target / f"{os.getpid()}.json").write_text(json.dumps(document), encoding="utf-8")

    atexit.register(report)
