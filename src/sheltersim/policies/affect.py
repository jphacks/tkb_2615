"""Installed, deterministic affect-transfer operators for typed social events.

These operators are experimental assumptions, not estimates of real populations.
They return bounded numeric contributions; they cannot mutate WorldState or author
dialogue. The body controller applies their results only for recorded events.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AffectContribution:
    positive_affect: float = 0.0
    negative_affect: float = 0.0
    reassurance: float = 0.0
    uncertainty: float = 0.0
    discontent: float = 0.0


class AffectPropagationPolicy(Protocol):
    model_id: str

    def contact(
        self, source: dict[str, Any], target: dict[str, Any], *, rate: float,
        relationship_trust: float,
    ) -> AffectContribution: ...

    def social_action(
        self, signal: float, *, rate: float, relationship_trust: float,
    ) -> AffectContribution: ...


class LinearAffectPropagationPolicy:
    """Transfer a bounded fraction of source/target affect difference."""

    model_id = "linear-positive-negative-v1"

    def contact(self, source: dict[str, Any], target: dict[str, Any], *, rate: float,
                relationship_trust: float) -> AffectContribution:
        del relationship_trust
        positive = _bounded_difference(source["positive_affect"], target["positive_affect"], rate)
        negative = _bounded_difference(source["negative_affect"], target["negative_affect"], rate)
        return AffectContribution(
            positive_affect=positive,
            negative_affect=negative,
            reassurance=max(0.0, positive - negative * 0.25),
            uncertainty=max(0.0, negative - positive) * 0.25,
        )

    def social_action(self, signal: float, *, rate: float,
                      relationship_trust: float) -> AffectContribution:
        del relationship_trust
        effect = rate * signal
        return AffectContribution(
            positive_affect=effect,
            negative_affect=-effect * 0.5,
            reassurance=effect,
            discontent=-effect * 0.5,
            uncertainty=-max(0.0, effect),
        )


class TrustWeightedAffectPropagationPolicy(LinearAffectPropagationPolicy):
    """Scale transfer by a recorded dyadic trust value, bounded to 0.5..1.5."""

    model_id = "trust-weighted-positive-negative-v1"

    def contact(self, source: dict[str, Any], target: dict[str, Any], *, rate: float,
                relationship_trust: float) -> AffectContribution:
        weighted_rate = rate * (0.5 + _unit(relationship_trust))
        return super().contact(source, target, rate=weighted_rate, relationship_trust=relationship_trust)

    def social_action(self, signal: float, *, rate: float,
                      relationship_trust: float) -> AffectContribution:
        weighted_rate = rate * (0.5 + _unit(relationship_trust))
        return super().social_action(signal, rate=weighted_rate, relationship_trust=relationship_trust)


_AFFECT_FACTORIES = {
    LinearAffectPropagationPolicy.model_id: LinearAffectPropagationPolicy,
    TrustWeightedAffectPropagationPolicy.model_id: TrustWeightedAffectPropagationPolicy,
}


def create_affect_policy(model_id: str) -> AffectPropagationPolicy:
    """Instantiate one allowlisted, code-installed affect operator."""
    factory = _AFFECT_FACTORIES.get(model_id)
    if factory is None:
        raise ValueError("body affect policy has no installed implementation")
    return factory()


def affect_policy_catalog() -> dict[str, dict[str, Any]]:
    """Version/formula/source metadata for later common Policy Registry wiring."""
    return {
        "linear-positive-negative-v1": {
            "implementation_version": "1",
            "formula": "delta = clamp((source - target) * rate, -rate, rate)",
            "source_id": "ASM-AFFECT-TRANSFER-V1",
            "reference": "assumption; no external calibration supplied",
            "coefficient": {"rate": {"unit": "fraction_per_recorded_contact", "lower": 0.0, "upper": 1.0}},
            "evidence": "assumption; no external calibration supplied",
            "external_validation": False,
        },
        "trust-weighted-positive-negative-v1": {
            "implementation_version": "1",
            "formula": "linear transfer rate multiplied by (0.5 + directed trust)",
            "source_id": "ASM-AFFECT-TRUST-WEIGHTING-V1",
            "reference": "assumption; no external calibration supplied",
            "coefficient": {
                "rate": {"unit": "fraction_per_recorded_contact", "lower": 0.0, "upper": 1.0},
                "relationship_trust": {"unit": "normalized_0_1", "lower": 0.0, "upper": 1.0},
            },
            "evidence": "assumption; no external calibration supplied",
            "external_validation": False,
        },
    }


def _bounded_difference(source: float, target: float, rate: float) -> float:
    return max(-rate, min(rate, (source - target) * rate))


def _unit(value: float) -> float:
    return min(1.0, max(0.0, value))
