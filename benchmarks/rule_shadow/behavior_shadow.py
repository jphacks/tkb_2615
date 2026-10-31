"""Compare the proposals of rules/behavior.rs with Python's at every call: see sitecustomize.py.

Each `_propose_due` runs the Python rule on the same view, then the kernel, and compares every field
of every proposal bit by bit: its type, a float's hex, the order of each dict's keys, the key of the
draw. The same for each `_observed_state`. The view is compared by world_view_shadow.py. Results are
in DIR/behavior, with what the runs reached: a branch no scenario takes is not compared.
"""

from __future__ import annotations

import atexit
import json
import os
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Any


def exact(value: Any, seen: Counter[str]) -> Any:
    """A form of the value in which two values are equal only with the same bits and types."""
    if value is None or type(value) is bool:
        return value
    if type(value) is float:
        seen["behavior_values_compared"] += 1
        return f"f{value.hex()}"
    if isinstance(value, Enum):
        seen["behavior_values_compared"] += 1
        return [type(value).__name__, value.value]
    if type(value) is int or type(value) is str:
        seen["behavior_values_compared"] += 1
        return [type(value).__name__, value]
    if isinstance(value, tuple):
        return ["tuple", *(exact(item, seen) for item in value)]
    if isinstance(value, list):
        return ["list", *(exact(item, seen) for item in value)]
    if isinstance(value, dict):
        return ["dict", *([key, exact(item, seen)] for key, item in value.items())]
    if hasattr(value, "__dataclass_fields__"):
        return [
            type(value).__name__,
            *([key, exact(item, seen)] for key, item in value.__dict__.items()),
        ]
    raise TypeError(f"a proposal holds a {type(value).__name__}")


def first_difference(python: str, rust: str) -> str:
    place = next(
        (i for i, (a, b) in enumerate(zip(python, rust, strict=False)) if a != b),
        min(len(python), len(rust)),
    )
    start, end = max(0, place - 120), place + 80
    return f"at {place}: python {python[start:end]!r} rust {rust[start:end]!r}"


def reached(proposals: Any, view: Any, seen: Counter[str]) -> None:
    for proposal in proposals:
        seen["proposals"] += 1
        seen[f"selected_{proposal.action.value}"] += 1
        seen[f"policy_{proposal.policy_version}"] += 1
        seen["selected_with_target"] += proposal.target_id is not None
        seen["ties_broken"] += proposal.rng_key[-1] == "priority-tie-break"
        seen["selected_with_priority"] += any(
            item.action is proposal.action and item.urgency_priority > 0
            for item in proposal.candidates
        )
        agent = view.agents[proposal.agent_id]
        seen["people_with_demand_modifiers"] += bool(agent.demand_modifiers)
        seen["people_with_context"] += agent.personal_context_present
        seen["people_with_request"] += agent.support_request_id is not None
        for item in proposal.candidates:
            if item.eligible:
                seen[f"eligible_{item.action.value}"] += 1
            if ";" in item.trigger and item.action.value != "seek_help":
                seen["triggers_with_demand"] += 1


def install(output: str) -> None:
    from sheltersim import native
    from sheltersim.experiments.phases.propose import ProposePhase
    from sheltersim.experiments.runner import OfflineSimulationController

    if not native.available():
        return
    seen: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    differences: list[str] = []
    rules = native.rules("behavior")
    propose_python = ProposePhase._propose_due
    observed_python = ProposePhase._observed_state

    def propose(self: Any, view: Any, rng: Any) -> Any:
        seen["propose_calls"] += 1
        python = python_error = None
        try:
            python = propose_python(self, view, rng)
        except Exception as error:  # noqa: BLE001 - only compared with what the kernel does
            python_error = error
        try:
            made = rules.propose_due(self, view, rng)
        except native.Unsupported as reason:
            reasons[f"propose_due: {reason}"] += 1
            if python_error is not None:
                raise python_error
            return python
        seen["propose_ran"] += 1
        if python_error is not None:
            differences.append(f"minute {view.minute_index}: the kernel took what Python raises on")
            return made
        left = json.dumps(exact(python, seen))
        right = json.dumps(exact(made, seen))
        if left != right:
            differences.append(f"minute {view.minute_index}: {first_difference(left, right)}")
        reached(made, view, seen)
        # A person with two queued tasks: which one the view reads may differ in a reversed run.
        queued: Counter[str] = Counter(
            task.agent_id
            for task in self.world.active_tasks.values()
            if task.agent_id is not None and task.kind in ("toilet_queue", "resource_service_queue")
        )
        seen["proposals_with_two_queued_tasks"] += sum(queued[p.agent_id] > 1 for p in made)
        return made

    def observed(self: Any, view: Any, agent_id: str) -> Any:
        seen["observed_calls"] += 1
        python = observed_python(self, view, agent_id)
        try:
            made = rules.observed_state(self, view, agent_id)
        except native.Unsupported as reason:
            reasons[f"observed_state: {reason}"] += 1
            return python
        seen["observed_ran"] += 1
        left = json.dumps(exact(python, seen))
        right = json.dumps(exact(made, seen))
        if left != right:
            differences.append(
                f"minute {view.minute_index}, {agent_id}: {first_difference(left, right)}"
            )
        seen["observed_with_queue"] += made["queue"]["task_id"] is not None
        seen["observed_with_request"] += made["support"]["request_id"] is not None
        seen["observed_with_environment"] += "zone_environment" in made
        return made

    OfflineSimulationController._propose_due = propose  # type: ignore[method-assign]
    OfflineSimulationController._observed_state = observed  # type: ignore[method-assign]

    target = Path(output) / "behavior"
    target.mkdir(parents=True, exist_ok=True)

    def report() -> None:
        if seen or reasons or differences:
            document = {"seen": seen, "unsupported": reasons, "differences": differences[:20]}
            (target / f"{os.getpid()}.json").write_text(json.dumps(document), encoding="utf-8")

    atexit.register(report)
