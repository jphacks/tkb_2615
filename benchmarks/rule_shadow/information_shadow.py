"""Compare the Rust information step with its Python rule at every call: every draw and probability.

sitecustomize.py installs this with the others; `summary.py DIR/information` adds up what it saw.
Each step runs the Python rule on a plain copy with a CounterRNG that records its draws, asks the
kernel what it plans (`planned_contacts`, which changes nothing and records its own draws), and then
runs the kernel on the world. It compares, as text in which a float keeps its bits:

- every draw, by key and value, including those of the partner sampling in contacts.rs;
- every contact: who, on what basis, for how long, which claim, with what probability, the outcome;
- the events the kernel schedules with the rule's proposals;
- the new namespace, with the row against the tree.
"""

from __future__ import annotations

import atexit
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any


def install(output: str) -> None:
    from sheltersim import native
    from sheltersim.core import state_mirror
    from sheltersim.core.engine import TraceLevel
    from sheltersim.core.rng import CounterRNG
    from sheltersim.domain.information import (
        INFORMATION_SPREAD_STATE_KEY as KEY,
    )
    from sheltersim.domain.information import (
        validate_information_runtime_state,
    )
    from sheltersim.experiments.phases.sample import SamplePhase
    from sheltersim.experiments.runner import OfflineSimulationController
    from sheltersim.policies.world_view import WorldView

    if not native.available():
        return
    seen: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    differences: list[str] = []
    rules = native.rules("information")
    python_step = SamplePhase._sample_information_contacts
    python_schedule = SamplePhase._schedule_information_contact
    rust_validate = native.state("information").validate
    scheduled: list[tuple[Any, ...]] = []

    class RecordingRNG(CounterRNG):
        def __init__(self, seed: int) -> None:
            super().__init__(seed)
            self.made: list[tuple[str, str]] = []

        def random(self, *key_parts: Any) -> float:
            value = super().random(*key_parts)
            key = json.dumps(list(key_parts), ensure_ascii=False, separators=(",", ":"))
            self.made.append((key, value.hex()))
            return value

    def schedule(self: Any, *arguments: Any) -> None:
        scheduled.append(arguments)
        python_schedule(self, *arguments)

    def note(minute: int, what: str) -> None:
        differences.append(f"minute {minute}: {what}")

    def step(self: Any, minute: int, rng: Any) -> None:
        seen["steps"] += 1
        policy = self.information_policy
        if policy is None:
            seen["no_model"] += 1
            rules.sample_contacts(self, minute, rng)
            return
        if self.artifacts.trace_level is not TraceLevel.OFF or type(rng) is not CounterRNG:
            seen["left_to_python_trace_or_rng"] += 1
            python_step(self, minute, rng)
            return
        world = self.world
        plain = json.loads(json.dumps(world.model_state[KEY]))
        recording = RecordingRNG(rng.seed)
        python_result = None
        python_error = None
        try:
            python_result = policy.update(
                WorldView.from_world(world, include_state_hash=False),
                plain,
                recording,
                zone_neighbors=self.zone_neighbors,
            )
        except Exception as error:  # noqa: BLE001 - only compared with what the kernel does
            python_error = error
        try:
            planned = rules.planned_contacts(self, minute, rng)
        except native.Unsupported as reason:
            reasons[str(reason)] += 1
            python_step(self, minute, rng)
            return
        if python_error is not None:
            note(minute, f"the kernel took what Python turned down: {python_error}")
            return
        seen[f"model {planned['policy_version']}"] += 1
        made = sorted((key, value.hex()) for key, value in planned["draws"])
        python_made = sorted(recording.made)
        if made != python_made:
            first = next((a, b) for a, b in zip([*made, None], [*python_made, None]) if a != b)
            note(minute, f"draws: {first}")
        for key, _ in python_made:
            seen["draws " + json.loads(key)[0]] += 1
        contacts = sorted(
            [tuple(item) for item in planned["contacts"]],
            key=lambda item: (item[1], item[0], item[4] or ""),
        )
        python_contacts = [
            (
                item.sender_agent_id,
                item.recipient_agent_id,
                item.contact_basis,
                item.contact_minutes,
                item.message_id,
                item.belief,
                item.transmission_probability,
                item.transmission_selected,
                item.message_source_id,
                item.causation_id,
            )
            for item in python_result.contacts
        ]
        if json.dumps(contacts) != json.dumps(python_contacts):
            first = next(
                ((a, b) for a, b in zip(contacts, python_contacts, strict=False) if a != b),
                (len(contacts), len(python_contacts)),
            )
            note(minute, f"contacts: {first}")
        seen["contacts"] += len(contacts)
        seen["probabilities"] += sum(item[6] > 0.0 for item in python_contacts)
        seen["contacts_family"] += sum(item[2] == "family" for item in python_contacts)
        seen["contacts_adjacent"] += sum(item[2] == "adjacent_zone" for item in python_contacts)
        seen["ticks_with_messages"] += bool(plain["messages"])
        seen["beliefs"] += sum(len(records) for records in plain["beliefs"].values())
        if python_result.policy_version != planned["policy_version"]:
            note(minute, "the model's version")

        scheduled.clear()
        rules.sample_contacts(self, minute, rng)
        seen["kernel_ran"] += 1
        state = world.model_state[KEY]
        if json.dumps(state, sort_keys=True) != json.dumps(
            python_result.runtime_state, sort_keys=True
        ):
            note(minute, KEY)
        if json.dumps(state) != json.dumps(python_result.runtime_state):
            for name, value in state.items():
                if json.dumps(value) != json.dumps(python_result.runtime_state[name]):
                    seen[f"another key order in {name}"] += 1
        forgotten = sum(
            before[message]["belief"] != "unknown"
            and python_result.runtime_state["beliefs"][agent][message]["belief"] == "unknown"
            for agent, before in plain["beliefs"].items()
            for message in before
        )
        seen["beliefs_forgotten"] += forgotten
        if not world._core.mirror(KEY).verify():
            note(minute, "the row and the tree")
        proposals = [
            (
                minute,
                item.message_id,
                item.sender_agent_id,
                item.recipient_agent_id,
                item.belief.value,
                item.contact_basis,
                item.contact_minutes,
                item.probability,
                item.message_source_id,
                item.causation_id,
            )
            for item in python_result.proposals
        ]
        if json.dumps(scheduled) != json.dumps(proposals):
            note(minute, f"proposals: {scheduled[:2]} and {proposals[:2]}")
        seen["proposals"] += len(proposals)
        seen["values_compared"] += (
            len(python_made) + len(contacts) * 10 + len(proposals) * 10 + _values(state)
        )

    def validator(world: Any) -> bool:
        accepted = rust_validate(world)
        seen["validator_accepted" if accepted else "validator_refused"] += 1
        if accepted:
            # Raises, and fails the run, if the kernel took what the Python rules turn down.
            validate_information_runtime_state(
                world.model_state[KEY],
                agent_ids=set(world.agents),
                zone_ids=set(world.zones),
                duration_minutes=world.duration_minutes,
            )
        return accepted

    state_mirror.register_rust_validator(KEY, validator)
    SamplePhase._schedule_information_contact = schedule  # type: ignore[method-assign]
    OfflineSimulationController._sample_information_contacts = step  # type: ignore[method-assign]

    def report() -> None:
        if seen or reasons or differences:
            directory = Path(output) / "information"
            directory.mkdir(parents=True, exist_ok=True)
            document = {"seen": seen, "unsupported": reasons, "differences": differences[:20]}
            (directory / f"{os.getpid()}.json").write_text(json.dumps(document), encoding="utf-8")

    atexit.register(report)


def _values(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_values(item) for item in value.values())
    if isinstance(value, list):
        return sum(_values(item) for item in value)
    return 1
