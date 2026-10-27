"""Installed alternative numerical operators; no evaluation of arbitrary code."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from sheltersim.policies.information import InformationSpreadPolicy
from sheltersim.policies.psychology import PsychologyUpdateResult, SimplePsychologyPolicy
from sheltersim.policies.social import StressPropagationPolicy


class BoundedStressPolicy(SimplePsychologyPolicy):
    """Exponential pressure/recovery avoids a linear stress saturation step."""

    policy_version = "bounded-stress-v1"

    def explain_update(self, view: Any, minute: int) -> PsychologyUpdateResult:
        baseline = super().explain_update(view, minute)
        deltas, traces = [], []
        for delta, trace in zip(baseline.deltas, baseline.traces, strict=True):
            components = [
                trace.need_pressure_stress,
                trace.queue_wait_stress,
                trace.support_wait_stress,
                trace.environmental_stress,
            ]
            pressure = sum(components)
            added = (1 - trace.stress_before) * -math.expm1(-pressure)
            scale = added / pressure if pressure else 0
            recovery = trace.stress_before * -math.expm1(trace.recovery_stress)
            stress = min(1.0, max(0.0, trace.stress_before + added - recovery))
            deltas.append(replace(delta, stress=stress))
            traces.append(
                replace(
                    trace,
                    stress_after=stress,
                    need_pressure_stress=components[0] * scale,
                    queue_wait_stress=components[1] * scale,
                    support_wait_stress=components[2] * scale,
                    environmental_stress=components[3] * scale,
                    recovery_stress=-recovery,
                )
            )
        return PsychologyUpdateResult(
            tuple(deltas), tuple(traces), policy_version=self.policy_version
        )


class DeGrootContactPolicy(StressPropagationPolicy):
    """Sparse consensus permits calming and amplification from the same contacts."""

    policy_version = "degroot-contact-v1"

    def update(
        self,
        view: Any,
        base_deltas: Any,
        rng: Any,
        *,
        zone_neighbors: Any,
        runtime_state: Any = None,
    ) -> Any:
        # Reuse contact eligibility, duration and RNG identity; replace influence operator.
        baseline = super().update(
            view, base_deltas, rng, zone_neighbors=zone_neighbors, runtime_state=runtime_state
        )
        if not self.parameters.enabled:
            return replace(baseline, policy_version=self.policy_version)
        eligible = {}
        for contact in baseline.contacts:
            if contact.consecutive_contact_minutes >= self.parameters.minimum_contact_minutes:
                eligible.setdefault(contact.recipient_id, []).append(contact)
        deltas, contacts = [], []
        total = 0.0
        for delta in base_deltas:
            neighbors = eligible.get(delta.agent_id, [])
            alpha = min(1.0, self.parameters.stress_spread_per_minute)
            contributions = [
                max(
                    -self.parameters.max_pair_stress_increase,
                    min(
                        self.parameters.max_pair_stress_increase,
                        alpha
                        * (view.agents[c.source_id].stress - view.agents[delta.agent_id].stress)
                        / max(1, len(neighbors)),
                    ),
                )
                for c in neighbors
            ]
            raw_shift = sum(contributions)
            limit = self.parameters.max_agent_stress_increase_per_minute
            shift = max(-limit, min(limit, raw_shift))
            recovered = max(0.0, delta.stress - self.parameters.natural_recovery_per_minute)
            applied = min(1.0, max(0.0, recovered + shift)) - recovered
            scale = applied / raw_shift if raw_shift else 1.0
            deltas.append(replace(delta, stress=recovered + applied))
            total += applied
            for contact, contribution in zip(neighbors, contributions, strict=True):
                contacts.append(
                    replace(
                        contact,
                        stress_difference=view.agents[contact.source_id].stress
                        - view.agents[delta.agent_id].stress,
                        stress_contribution=contribution * scale,
                        cooldown_remaining_minutes=0,
                    )
                )
        state = {**baseline.runtime_state, "agent_cooldown_until": {}}
        return replace(
            baseline,
            deltas=tuple(deltas),
            runtime_state=state,
            contacts=tuple(contacts),
            total_stress_added=total,
            contributing_contact_count=sum(c.stress_contribution != 0 for c in contacts),
            policy_version=self.policy_version,
        )


class HazardInformationPolicy(InformationSpreadPolicy):
    policy_version = "hazard-information-v1"

    def update(self, *args: Any, **kwargs: Any) -> Any:
        return replace(super().update(*args, **kwargs), policy_version=self.policy_version)

    def _transmission_probability(
        self, trust: float, basis: str, contact_minutes: int, *, correction: bool
    ) -> float:
        propensity = super()._transmission_probability(
            trust, basis, contact_minutes, correction=correction
        )
        return -math.expm1(-propensity)
