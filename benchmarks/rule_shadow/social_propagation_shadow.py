"""Compare the Rust social step with its Python rules at every call: see sitecustomize.py.

Each step runs the policy's Python rule first, on a plain copy of the namespace and with an RNG
that keeps its draws, then the kernel on the world. What must be the same: the deltas and the
namespace as JSON text (a float keeps its bits, 1 differs from 1.0, and keys keep their order),
every draw with its key and bits, and what the contacts did to social_network_v1, which Rust now
records itself: the Python rule's records on a plain copy of the ones touched. The row must write
the bytes of the tree.

A run that reverses dicts (SHELTERSIM_GATE_REVERSE, gate_all_paths.py --reversed-order) changes the
order of the tree's keys on purpose, and not the Python rule's new dicts, so there the comparison
ignores key order. Any other run reports a change of order.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any


def _text(value: Any) -> str:
    reversing = bool(os.environ.get("SHELTERSIM_GATE_REVERSE"))
    return json.dumps(value, ensure_ascii=False, sort_keys=reversing)


def install(seen: Counter[str], reasons: Counter[str], differences: list[str]) -> None:
    from sheltersim import native
    from sheltersim.core.engine import TraceLevel
    from sheltersim.core.rng import CounterRNG
    from sheltersim.domain import social_network
    from sheltersim.domain.models import ordered_pair_key
    from sheltersim.experiments.phases.update import UpdatePhase
    from sheltersim.experiments.runner import OfflineSimulationController
    from sheltersim.policies.social import SOCIAL_RUNTIME_STATE_KEY

    rules = native.rules("social_propagation")
    kernel = rules.update_social_propagation
    python_step = UpdatePhase._social_propagation
    network_key = social_network.SOCIAL_NETWORK_STATE_KEY

    class KeepingRNG(CounterRNG):
        def __init__(self, seed: int) -> None:
            super().__init__(seed)
            self.draws: list[tuple[str, str]] = []

        def random(self, *key_parts: Any) -> float:
            value = super().random(*key_parts)
            key = json.dumps(list(key_parts), ensure_ascii=False, separators=(",", ":"))
            self.draws.append((key, value.hex()))
            return value

    def step(self: Any, world: Any, view: Any, base_deltas: Any, minute: int, rng: Any) -> Any:
        seen["social_steps"] += 1
        policy = self.stress_policy
        if not policy.parameters.enabled:
            seen["social_disabled"] += 1
            if kernel(self, world, view, base_deltas, minute, rng) is not base_deltas:
                differences.append(f"minute {minute}: a disabled model changed the deltas")
            return base_deltas
        python = None
        keeping = None
        if self.artifacts.trace_level is TraceLevel.OFF and type(rng) is CounterRNG:
            keeping = KeepingRNG(rng.seed)
            plain = json.loads(json.dumps(world.model_state.get(SOCIAL_RUNTIME_STATE_KEY)))
            try:
                python = policy.update(
                    view,
                    base_deltas,
                    keeping,
                    zone_neighbors=self.zone_neighbors,
                    runtime_state=plain,
                )
            except Exception:  # noqa: BLE001 - only compared with what the kernel does
                python = None
        network = world.model_state.get(network_key)
        network_before = None
        if python is not None and network is not None:
            touched = {
                ordered_pair_key(contact.recipient_id, contact.source_id)
                for contact in python.contacts
            }
            network_before = json.loads(
                json.dumps(
                    {
                        **network,
                        "relationships": {
                            key: network["relationships"][key]
                            for key in network["relationships"]
                            if key in touched
                        },
                    }
                )
            )
        rules.start_draw_log()
        try:
            final = kernel(self, world, view, base_deltas, minute, rng)
        except native.Unsupported as reason:
            rules.take_draw_log()
            reasons[f"social: {reason}"] += 1
            return python_step(self, world, view, base_deltas, minute, rng)
        rust_draws = rules.take_draw_log()
        seen["social_kernel_ran"] += 1
        seen[f"social_ran_{policy.policy_version}"] += 1
        if python is None:
            differences.append(f"minute {minute}: the kernel took what Python turned down")
            return final
        state = world.model_state[SOCIAL_RUNTIME_STATE_KEY]
        if _text(python.runtime_state) != _text(state):
            differences.append(f"minute {minute}: {SOCIAL_RUNTIME_STATE_KEY}")
        rust_deltas = _text([asdict(delta) for delta in final])
        if _text([asdict(delta) for delta in python.deltas]) != rust_deltas:
            differences.append(f"minute {minute}: the deltas")
        if [(key, value.hex()) for key, value in rust_draws] != keeping.draws:
            differences.append(f"minute {minute}: the draws")
        if network_before is not None:
            for contact in python.contacts:
                social_network.record_observed_contact(
                    SimpleNamespace(model_state={network_key: network_before}),
                    recipient_id=contact.recipient_id,
                    source_id=contact.source_id,
                    minute=minute,
                )
            now = world.model_state[network_key]
            kept = {
                **now,
                "relationships": {
                    key: now["relationships"][key]
                    for key in now["relationships"]
                    if key in network_before["relationships"]
                },
            }
            if json.dumps(network_before, sort_keys=True) != json.dumps(kept, sort_keys=True):
                differences.append(f"minute {minute}: the observed contacts")
            if not world._core.mirror(network_key).verify():
                differences.append(f"minute {minute}: the row and the tree of social_network_v1")
            seen["social_observed_compared"] += len(python.contacts)
        if not world._core.mirror(SOCIAL_RUNTIME_STATE_KEY).verify():
            differences.append(f"minute {minute}: the row and the tree of social")
        seen["social_draws_compared"] += len(rust_draws)
        seen["social_contacts_compared"] += len(state["contacts"])
        seen["social_cooldowns_compared"] += len(state["agent_cooldown_until"])
        seen["social_deltas_compared"] += len(final)
        seen["social_stress_changed"] += sum(
            base.stress != after.stress
            for base, after in zip(
                sorted(base_deltas, key=lambda d: d.agent_id), final, strict=True
            )
        )
        return final

    OfflineSimulationController._social_propagation = step  # type: ignore[method-assign]
