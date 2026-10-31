"""The shadow of the Rust psychology steps: a minute's new values and writing them.

sitecustomize.py installs this. Each `_psychology_update` runs the Python rules on the same view
first (`explain_update`, and `deltas_without_traces` where the model has it), then the kernel, and
compares every field as the text of JSON. Each `_apply_state_deltas` runs `apply_state_deltas` on a
plain stand-in of the people and their body records first, then the kernel on the world, and
compares every value and the rows.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import fields
from types import SimpleNamespace
from typing import Any

_LIFE_STATE = ("life-state-feedback-v1", "mean-reverting-life-state-v1")


def _delta_text(delta: Any) -> str:
    return json.dumps([getattr(delta, item.name) for item in fields(delta)], ensure_ascii=False)


def install(seen: Counter[str], reasons: Counter[str], differences: list[str]) -> None:
    from sheltersim import native
    from sheltersim.core.engine import TraceLevel
    from sheltersim.domain.models import Activity
    from sheltersim.experiments.phases.update import UpdatePhase
    from sheltersim.experiments.runner import OfflineSimulationController
    from sheltersim.policies.psychology import apply_state_deltas

    rules = native.rules("psychology")
    python_update = UpdatePhase._psychology_update
    python_apply = UpdatePhase._apply_state_deltas

    def reach(view: Any, policy: Any) -> None:
        version = getattr(policy, "policy_version", "?")
        seen[f"update_model:{version}"] += 1
        for agent in view.agents.values():
            seen["update_people"] += 1
            if agent.activity is Activity.SLEEPING:
                seen["update_sleeping"] += 1
            elif agent.activity is Activity.RESTING:
                seen["update_resting"] += 1
            elif agent.activity is Activity.QUEUED:
                seen["update_queued"] += 1
            if agent.support_request_status in ("pending", "assigned"):
                seen["update_support_open"] += 1
            if agent.required_sleep_minutes is not None:
                seen["update_required_sleep"] += 1
            if agent.misinformation_belief_pressure:
                seen["update_misinformation"] += 1
            if agent.environmental_stress_per_minute:
                seen["update_environmental_stress"] += 1
            if version in _LIFE_STATE:
                peers = view.group_members.get(agent.group_id or "", ())
                if any(
                    peer != agent.id and view.agents[peer].zone_id != agent.zone_id
                    for peer in peers
                ):
                    seen["update_family_separated"] += 1

    def update(self: Any, view: Any, minute: int) -> Any:
        seen["update_steps"] += 1
        policy = self.psychology
        expected = None
        error = None
        untraced = self.artifacts.trace_level is TraceLevel.OFF
        if untraced:
            try:
                expected = [policy.explain_update(view, minute).deltas]
                without_traces = getattr(policy, "deltas_without_traces", None)
                if without_traces is not None:
                    expected.append(without_traces(view, minute))
            except Exception as caught:  # noqa: BLE001 - only compared with what the kernel does
                error = caught
        try:
            result = rules.update_psychology(self, view, minute)
        except native.Unsupported as reason:
            reasons[f"update: {reason}"] += 1
            return python_update(self, view, minute)
        seen["update_kernel_ran"] += 1
        if error is not None:
            differences.append(f"minute {minute}: the kernel took a view Python refused: {error}")
            return result
        reach(view, policy)
        mine = [_delta_text(delta) for delta in result.deltas]
        for number, python in enumerate(expected or ()):
            theirs = [_delta_text(delta) for delta in python]
            if theirs != mine:
                differences.append(f"minute {minute}: deltas {number} of {policy.policy_version}")
            seen["update_values_compared"] += 15 * len(theirs)
        return result

    def apply(self: Any, world: Any, final_deltas: Any) -> None:
        seen["apply_steps"] += 1
        body = world.model_state.get("body_state_v1")
        records = body.get("agents") if isinstance(body, dict) else None
        standin = SimpleNamespace(
            minute_index=world.minute_index,
            agents={
                key: SimpleNamespace(
                    stress=agent.stress,
                    trust=agent.trust,
                    needs=SimpleNamespace(**agent.needs.to_dict()),
                )
                for key, agent in world.agents.items()
            },
            model_state=(
                {"body_state_v1": {"agents": copy.deepcopy(dict(records))}}
                if records is not None
                else {}
            ),
        )
        error = None
        try:
            apply_state_deltas(standin, final_deltas)
        except Exception as caught:  # noqa: BLE001 - only compared with what the kernel does
            error = caught
        try:
            rules.apply_deltas(self, world, final_deltas)
        except native.Unsupported as reason:
            reasons[f"apply: {reason}"] += 1
            python_apply(self, world, final_deltas)
            return
        seen["apply_kernel_ran"] += 1
        if error is not None:
            differences.append(f"minute {world.minute_index}: the kernel took what Python refused")
            return
        for key, agent in world.agents.items():
            other = standin.agents[key]
            theirs = [other.stress, other.trust, *vars(other.needs).values()]
            mine = [agent.stress, agent.trust, *agent.needs.to_dict().values()]
            if json.dumps(theirs) != json.dumps(mine):
                differences.append(f"minute {world.minute_index}: the person {key}")
            seen["apply_values_compared"] += len(theirs)
        if records is not None:
            if json.dumps(standin.model_state["body_state_v1"]["agents"]) != json.dumps(records):
                differences.append(f"minute {world.minute_index}: body records")
            seen["apply_values_compared"] += sum(len(item) for item in records.values())
        store = world._core.entities()
        problem = store.verify(world)
        if problem is not None or store.tainted is not None:
            differences.append(f"minute {world.minute_index}: rows: {problem or store.tainted}")

    OfflineSimulationController._psychology_update = update  # type: ignore[method-assign]
    OfflineSimulationController._apply_state_deltas = apply  # type: ignore[method-assign]
