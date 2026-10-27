"""Exchangeable trust formation operators for measured Agent contact events."""

from __future__ import annotations

from typing import Protocol


class RelationshipFormationPolicy(Protocol):
    model_id: str
    source_id: str
    implementation_version: str
    formula: str

    def update_trust(
        self,
        current: float,
        *,
        duration_minutes: float,
        interaction_scale_minutes: float,
        prior_contact_count: int,
        elapsed_minutes: int,
        trust_gain: float,
        trust_decay_per_day: float,
        distance_m: float | None,
    ) -> float: ...


class DistanceDurationRepeatTrustPolicy:
    """Build familiarity from closeness, contact duration, repetition, and elapsed time."""

    model_id = "distance-duration-repeat-trust-v1"
    source_id = "ASM-DISTANCE-DURATION-REPEAT-TRUST-V1"
    implementation_version = "1"
    formula = "baseline = trust*(1-decay*elapsed/day)+0.5*decay*elapsed/day; gain = contact_gain*min(1,duration/scale)*(1+repeat_bonus*min(prior_contacts,10))/(1+observed_distance/distance_scale); missing observed distance uses factor 1"

    def __init__(
        self, *, distance_scale_m: float = 2.0, repeat_contact_bonus: float = 0.05
    ) -> None:
        self.distance_scale_m = distance_scale_m
        self.repeat_contact_bonus = repeat_contact_bonus

    def update_trust(
        self,
        current: float,
        *,
        duration_minutes: float,
        interaction_scale_minutes: float,
        prior_contact_count: int,
        elapsed_minutes: int,
        trust_gain: float,
        trust_decay_per_day: float,
        distance_m: float | None,
    ) -> float:
        decay = min(1.0, max(0.0, trust_decay_per_day * elapsed_minutes / 1440.0))
        baseline = current * (1.0 - decay) + 0.5 * decay
        distance_factor = (
            1.0 / (1.0 + max(0.0, distance_m) / self.distance_scale_m)
            if distance_m is not None
            else 1.0
        )
        repeat_factor = 1.0 + self.repeat_contact_bonus * min(max(prior_contact_count, 0), 10)
        duration_factor = min(1.0, max(0.0, duration_minutes) / max(1.0, interaction_scale_minutes))
        result = baseline + trust_gain * duration_factor * repeat_factor * distance_factor
        return min(1.0, max(0.0, result))


class DurationOnlyTrustPolicy:
    """Reference alternative retaining the integrated contact-duration formula."""

    model_id = "duration-contact-trust-v1"
    source_id = "ASM-DURATION-CONTACT-TRUST-V1"
    implementation_version = "1"
    formula = "baseline = trust*(1-decay*elapsed/day)+0.5*decay*elapsed/day; gain = contact_gain*min(1,duration/scale)"

    def update_trust(
        self,
        current: float,
        *,
        duration_minutes: float,
        interaction_scale_minutes: float,
        prior_contact_count: int,
        elapsed_minutes: int,
        trust_gain: float,
        trust_decay_per_day: float,
        distance_m: float | None,
    ) -> float:
        del prior_contact_count, distance_m
        decay = min(1.0, max(0.0, trust_decay_per_day * elapsed_minutes / 1440.0))
        baseline = current * (1.0 - decay) + 0.5 * decay
        duration_factor = min(1.0, max(0.0, duration_minutes) / max(1.0, interaction_scale_minutes))
        return min(1.0, max(0.0, baseline + trust_gain * duration_factor))


def create_relationship_policy(
    configs: dict[str, dict[str, object]],
) -> RelationshipFormationPolicy:
    """Resolve an explicit Registry replacement or the current proximity model."""
    selection = configs.get("policy_registry", {}).get("relationship")
    if selection is not None:
        from sheltersim.policies.registry import create_installed_model

        return create_installed_model("relationship", configs)
    # Keep the historical duration-only operator active unless a scenario opts in
    # through the shared registry. This makes the new distance/repeat formula
    # visible in model_signatures whenever it changes a run.
    return DurationOnlyTrustPolicy()
