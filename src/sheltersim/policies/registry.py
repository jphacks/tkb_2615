"""Explicit factories for installed models; no dynamic imports or expressions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from sheltersim.contracts.policies import (
    MODEL_DEFINITIONS,
    model_catalog,
    model_signatures,
    selected_models,
)
from sheltersim.domain.models import InformationSpreadConfig
from sheltersim.domain.psychological_state import INTEGRATED_PSYCHOLOGY_POLICY_VERSION
from sheltersim.experiments.scenario import PsychologyModelConfig
from sheltersim.policies.alternatives import (
    BoundedStressPolicy,
    DeGrootContactPolicy,
    HazardInformationPolicy,
)
from sheltersim.policies.behavior import BehaviorPolicy, create_behavior_policy
from sheltersim.policies.information import EvidenceAwareInformationPolicy, InformationSpreadPolicy
from sheltersim.policies.psychology import (
    IntegratedDistressLifeStatePolicy,
    LifeStatePsychologyPolicy,
    MeanRevertingDistressPolicy,
    PsychologyParameters,
    PsychologyPolicy,
    SimplePsychologyPolicy,
)
from sheltersim.policies.social import (
    ExpressivenessChannelOpennessPolicy,
    SocialPropagationParameters,
    StressPropagationPolicy,
)

# Installed factories only. There is no scenario-supplied import or eval path.
_INSTALLED_FACTORIES: dict[tuple[str, str], Callable[..., Any]] = {}


@dataclass(frozen=True)
class BodyDynamicsFormula:
    """One installed stress-feedback equation for the body-state policy."""

    model_id: str
    source_id: str

    def stress_feedback(
        self,
        *,
        sleep_debt_minutes: float,
        uncertainty: float,
        discontent: float,
        hygiene_need: float,
        sanitation_level: float,
        family_separation_pressure: float,
        reassurance: float,
        positive_affect: float,
        config: dict[str, Any],
        agent_id: str | None = None,
    ) -> float:
        per_agent = config.get("sleep_debt_stress_rate_by_agent", {})
        sleep_debt_rate = per_agent.get(agent_id, config["sleep_debt_stress_rate"])
        debt_component = sleep_debt_minutes / 1440.0 * sleep_debt_rate
        if self.model_id == "body-feedback-sleep-debt-v1":
            return debt_component
        negative_affect_rates = config.get("negative_affect_stress_rate_by_agent", {})
        negative_affect_rate = negative_affect_rates.get(
            agent_id, config["negative_affect_stress_rate"]
        )
        discontent_rates = config.get("discontent_stress_rate_by_agent", {})
        discontent_rate = discontent_rates.get(agent_id, config["discontent_stress_rate"])
        return (
            debt_component
            + uncertainty * negative_affect_rate
            + discontent * discontent_rate
            + hygiene_need * config["sanitation_stress_rate"]
            + (1.0 - sanitation_level) * config["sanitation_stress_rate"]
            + family_separation_pressure
            - reassurance * negative_affect_rate
            - positive_affect * config["discontent_relief_per_minute"]
        )


@dataclass(frozen=True)
class CareOutcomeFormula:
    """Installed experimental responses for typed, non-clinical care state."""

    model_id: str
    source_id: str
    config: dict[str, Any]

    def hygiene_need_after_support(self, current_need: float, relief: float) -> float:
        bounded_need = min(1.0, max(0.0, current_need))
        bounded_relief = min(1.0, max(0.0, relief))
        if self.model_id == "hygiene-support-proportional-relief-v1":
            return bounded_need * (1.0 - bounded_relief)
        return max(0.0, bounded_need - bounded_relief)

    def support_state_deltas(
        self, support_kind: str, *, agent_id: str | None = None
    ) -> dict[str, float]:
        """Return bounded state deltas only for the selected typed-care formula."""
        if self.model_id != "typed-care-support-response-v1":
            return {}

        def configured_value(name: str, default: float) -> float:
            per_agent = self.config.get(f"{name}_by_agent", {})
            if agent_id is not None and isinstance(per_agent, dict) and agent_id in per_agent:
                return float(per_agent[agent_id])
            return float(self.config.get(name, default))

        responses = {
            "caregiver_assistance": {
                "agent.stress": -configured_value("caregiver_assistance_stress_relief", 0.03),
                "body_state_v1.agents.reassurance": configured_value(
                    "caregiver_assistance_reassurance_gain", 0.02
                ),
            },
            "dependent_assistance": {
                "agent.stress": -configured_value("dependent_assistance_stress_relief", 0.04),
                "body_state_v1.agents.reassurance": configured_value(
                    "dependent_assistance_reassurance_gain", 0.03
                ),
            },
            "privacy_accommodation": {
                "body_state_v1.agents.discontent": -configured_value(
                    "privacy_accommodation_discontent_relief", 0.02
                ),
            },
            "mobility_assistance": {
                "agent.stress": -configured_value("mobility_assistance_stress_relief", 0.03),
            },
            "language_access": {
                "body_state_v1.agents.uncertainty": -configured_value(
                    "language_access_uncertainty_relief", 0.05
                ),
            },
            # These are uncertainty responses to recorded support access only.
            # They do not represent assessment findings, medication use, or health.
            "medical_assessment": {
                "body_state_v1.agents.uncertainty": -configured_value(
                    "medical_assessment_uncertainty_relief", 0.02
                ),
            },
            "medication_support": {
                "body_state_v1.agents.uncertainty": -configured_value(
                    "medication_support_uncertainty_relief", 0.01
                ),
            },
        }
        return dict(responses.get(support_kind, {}))


@dataclass(frozen=True)
class StaffOperationsFormula:
    """Installed deterministic ranking rule for routed staff assignments."""

    model_id: str
    source_id: str

    def rank_key(
        self,
        urgency: float,
        travel_minutes: int,
        task_kind: str,
        zone_id: str,
        target_id: str | None,
    ) -> tuple[float, float, str, str, str]:
        stable_key = (task_kind, zone_id, target_id or "")
        if self.model_id == "staff-travel-first-v1":
            return (-travel_minutes, urgency, *stable_key)
        return (urgency, -travel_minutes, *stable_key)


@dataclass(frozen=True)
class StaffFatigueFormula:
    """Installed duty-duration to normalized fatigue equation."""

    model_id: str
    source_id: str
    max_work_minutes_by_staff: dict[str, int]

    def maximum_minutes(self, default_minutes: int, staff_id: str) -> int:
        return self.max_work_minutes_by_staff.get(staff_id, default_minutes)

    def fatigue(self, consecutive_work_minutes: int, maximum_minutes: int) -> float:
        fraction = consecutive_work_minutes / maximum_minutes
        if self.model_id == "staff-fatigue-saturating-v1":
            import math

            return 1.0 - math.exp(-fraction)
        return min(1.0, fraction)


@dataclass(frozen=True)
class ZoneEnvironmentFormula:
    """Installed temperature-to-stress equation for relative indoor conditions."""

    model_id: str
    source_id: str

    def temperature_deviation(self, temperature_c: float, config: dict[str, Any]) -> float:
        minimum = config["comfortable_min_temperature_c"]
        maximum = config["comfortable_max_temperature_c"]
        if self.model_id == "zone-temperature-midpoint-v1":
            return abs(temperature_c - (minimum + maximum) / 2.0)
        return max(0.0, minimum - temperature_c, temperature_c - maximum)

    def rest_efficiency(
        self,
        noise_level: float,
        lighting_level: float,
        config: dict[str, Any],
    ) -> float:
        noise_loss = noise_level * config["noise_rest_efficiency_loss"]
        lighting_loss = lighting_level * config["lighting_sleep_efficiency_loss"]
        if self.model_id == "zone-rest-limiting-v1":
            return max(0.1, 1.0 - max(noise_loss, lighting_loss))
        return max(0.1, (1.0 - noise_loss) * (1.0 - lighting_loss))

    def ventilation_hygiene_growth(self, ventilation: float, config: dict[str, Any]) -> float:
        deficit = 1.0 - ventilation
        if self.model_id == "zone-hygiene-quadratic-v1":
            deficit *= deficit
        return deficit * config["ventilation_hygiene_growth_per_minute"]

    def privacy_discontent(self, privacy: float, config: dict[str, Any]) -> float:
        deficit = (
            1.0 - privacy
            if self.model_id == "zone-privacy-proportional-v1"
            else max(0.0, 0.4 - privacy)
        )
        return deficit * config["privacy_discontent_per_minute"]


def _body_formula_factory(model_id: str) -> Callable[..., BodyDynamicsFormula]:
    def factory(*, configs: dict[str, dict[str, Any]], **kwargs: Any) -> BodyDynamicsFormula:
        del configs, kwargs
        return BodyDynamicsFormula(model_id, MODEL_DEFINITIONS[model_id]["assumption_source_id"])

    return factory


def _staff_formula_factory(model_id: str) -> Callable[..., StaffOperationsFormula]:
    def factory(*, configs: dict[str, dict[str, Any]], **kwargs: Any) -> StaffOperationsFormula:
        del configs, kwargs
        return StaffOperationsFormula(model_id, MODEL_DEFINITIONS[model_id]["assumption_source_id"])

    return factory


def _environment_formula_factory(model_id: str) -> Callable[..., ZoneEnvironmentFormula]:
    def factory(*, configs: dict[str, dict[str, Any]], **kwargs: Any) -> ZoneEnvironmentFormula:
        del configs, kwargs
        return ZoneEnvironmentFormula(model_id, MODEL_DEFINITIONS[model_id]["assumption_source_id"])

    return factory


def _staff_fatigue_formula_factory(model_id: str) -> Callable[..., StaffFatigueFormula]:
    def factory(*, configs: dict[str, dict[str, Any]], **kwargs: Any) -> StaffFatigueFormula:
        del kwargs
        config = configs.get("staff_dynamics", {})
        return StaffFatigueFormula(
            model_id,
            MODEL_DEFINITIONS[model_id]["assumption_source_id"],
            dict(config.get("max_continuous_work_minutes_by_staff", {})),
        )

    return factory


_INSTALLED_FACTORIES.update(
    {
        ("body_dynamics", model_id): _body_formula_factory(model_id)
        for model_id in (
            "body-feedback-complete-v1",
            "body-feedback-complete-v2",
            "body-feedback-sleep-debt-v1",
        )
    }
)


def _care_outcome_formula_factory(model_id: str) -> Callable[..., CareOutcomeFormula]:
    def factory(*, configs: dict[str, dict[str, Any]], **kwargs: Any) -> CareOutcomeFormula:
        del kwargs
        return CareOutcomeFormula(
            model_id,
            MODEL_DEFINITIONS[model_id]["assumption_source_id"],
            dict(configs.get("body_dynamics", {})),
        )

    return factory


_INSTALLED_FACTORIES.update(
    {
        ("care_outcome", model_id): _care_outcome_formula_factory(model_id)
        for model_id in (
            "hygiene-support-fixed-relief-v1",
            "hygiene-support-proportional-relief-v1",
            "typed-care-support-response-v1",
        )
    }
)
_INSTALLED_FACTORIES.update(
    {
        ("staff_fatigue", model_id): _staff_fatigue_formula_factory(model_id)
        for model_id in ("staff-fatigue-linear-v1", "staff-fatigue-saturating-v1")
    }
)
_INSTALLED_FACTORIES.update(
    {
        ("staff_operations", model_id): _staff_formula_factory(model_id)
        for model_id in ("staff-urgency-first-v1", "staff-travel-first-v1")
    }
)
_INSTALLED_FACTORIES.update(
    {
        (domain, model_id): _environment_formula_factory(model_id)
        for domain, model_ids in {
            "zone_rest": ("zone-rest-multiplicative-v1", "zone-rest-limiting-v1"),
            "zone_hygiene": ("zone-hygiene-linear-v1", "zone-hygiene-quadratic-v1"),
            "zone_privacy": ("zone-privacy-threshold-v1", "zone-privacy-proportional-v1"),
        }.items()
        for model_id in model_ids
    }
)
_INSTALLED_FACTORIES.update(
    {
        ("zone_environment", model_id): _environment_formula_factory(model_id)
        for model_id in (
            "zone-temperature-comfort-band-v1",
            "zone-temperature-midpoint-v1",
        )
    }
)


def register_policy(model_id: str, definition: dict[str, Any], factory: Callable[..., Any]) -> None:
    from pydantic import TypeAdapter

    from sheltersim.contracts.extensions import Identifier
    from sheltersim.contracts.policies import PolicyModelDefinition

    TypeAdapter(Identifier).validate_python(model_id)
    required = {
        "domain",
        "reference",
        "formula",
        "assumption_source_id",
        "implementation_version",
        "state_schema_version",
    }
    if not required <= set(definition) or not callable(factory) or model_id in MODEL_DEFINITIONS:
        raise ValueError("incomplete or duplicate installed policy")
    if definition.get("external_validation") not in (None, False):
        raise ValueError("installed code registration cannot certify external validity")
    TypeAdapter(Identifier).validate_python(definition["domain"])
    TypeAdapter(Identifier).validate_python(definition["implementation_version"])
    TypeAdapter(Identifier).validate_python(definition["assumption_source_id"])
    if any(
        not isinstance(definition[name], str) or not definition[name]
        for name in ("reference", "formula")
    ):
        raise ValueError("installed policies require model reference and formula metadata")
    import json

    json.dumps(definition, allow_nan=False)
    for path, metadata in definition.get("parameters", {}).items():
        if "." not in path or not isinstance(metadata.get("unit"), str) or not metadata["unit"]:
            raise ValueError("installed parameter metadata requires a config path and unit")
    if (
        type(definition["state_schema_version"]) is not int
        or definition["state_schema_version"] < 1
    ):
        raise ValueError("installed policy requires a positive state schema version")
    # Registration validates versioned formula/parameter/source metadata before a
    # model is selectable; metadata alone still does not install executable code.
    PolicyModelDefinition.model_validate(definition)
    requirements = definition.get("required_state_contracts", {})
    if not isinstance(requirements, dict):
        raise ValueError("model state requirements must be an object")
    for namespace, signature in requirements.items():
        TypeAdapter(Identifier).validate_python(namespace)
        if (
            not isinstance(signature, dict)
            or set(signature) != {"implementation_id", "implementation_version", "schema_version"}
            or type(signature["schema_version"]) is not int
            or signature["schema_version"] < 1
        ):
            raise ValueError("invalid installed model state requirement")
        TypeAdapter(Identifier).validate_python(signature["implementation_id"])
        TypeAdapter(Identifier).validate_python(signature["implementation_version"])
    from copy import deepcopy

    MODEL_DEFINITIONS[model_id] = deepcopy(definition)
    _INSTALLED_FACTORIES[(definition["domain"], model_id)] = factory


_BUILTIN_FACTORIES = {
    "behavior": frozenset(
        {"utility-softmax-v1", "sleep-aware-utility-softmax-v2", "priority-rule-v1"}
    ),
    "psychology": frozenset(
        {
            "causal-stress-v1",
            "bounded-stress-v1",
            "life-state-feedback-v1",
            "mean-reverting-distress-v1",
            INTEGRATED_PSYCHOLOGY_POLICY_VERSION,
        }
    ),
    "social": frozenset(
        {
            "sparse-family-zone-contact-v1",
            "degroot-contact-v1",
            "expressiveness-channel-openness-v1",
        }
    ),
    "information": frozenset(
        {"sparse-information-contact-v1", "hazard-information-v1", "evidence-aware-rumor-v1"}
    ),
}


def has_installed_factory(domain: str, model_id: str) -> bool:
    """Metadata alone cannot authorize a nonexistent numerical operator."""
    return (
        model_id in _BUILTIN_FACTORIES.get(domain, ()) or (domain, model_id) in _INSTALLED_FACTORIES
    )


def create_installed_model(domain: str, configs: dict[str, dict[str, Any]], **kwargs: Any) -> Any:
    selected = selected_models(configs).get(domain)
    factory = _INSTALLED_FACTORIES.get((domain, selected))
    if factory is None:
        raise ValueError("requested model has no installed factory")
    return factory(configs=configs, **kwargs)


def create_body_dynamics_formula(configs: dict[str, dict[str, Any]]) -> BodyDynamicsFormula:
    """Create the selected body formula, retaining the historical full-feedback default."""
    selected = selected_models(configs).get("body_dynamics", "body-feedback-complete-v1")
    return _INSTALLED_FACTORIES[("body_dynamics", selected)](configs=configs)


def create_care_outcome_formula(
    configs: dict[str, dict[str, Any]],
) -> CareOutcomeFormula | None:
    """Create a selected care-response formula or retain the legacy fixed path."""
    selected = selected_models(configs).get("care_outcome")
    if selected is None:
        return None
    return _INSTALLED_FACTORIES[("care_outcome", selected)](configs=configs)


def create_staff_operations_formula(
    configs: dict[str, dict[str, Any]],
) -> StaffOperationsFormula:
    """Create the selected routed staff-task ranking rule."""
    selected = configs.get("policy_registry", {}).get("staff_operations", "staff-urgency-first-v1")
    return _INSTALLED_FACTORIES[("staff_operations", selected)](configs=configs)


def create_staff_fatigue_formula(
    configs: dict[str, dict[str, Any]],
) -> StaffFatigueFormula:
    """Create the selected staff duty-fatigue equation."""
    selected = configs.get("policy_registry", {}).get("staff_fatigue", "staff-fatigue-linear-v1")
    return _INSTALLED_FACTORIES[("staff_fatigue", selected)](configs=configs)


def create_zone_environment_formula(
    configs: dict[str, dict[str, Any]],
) -> ZoneEnvironmentFormula:
    """Create the selected temperature exposure calculation."""
    selected = configs.get("policy_registry", {}).get(
        "zone_environment", "zone-temperature-comfort-band-v1"
    )
    return _INSTALLED_FACTORIES[("zone_environment", selected)](configs=configs)


def create_zone_rest_formula(configs: dict[str, dict[str, Any]]) -> ZoneEnvironmentFormula:
    selected = configs.get("policy_registry", {}).get("zone_rest", "zone-rest-multiplicative-v1")
    return _INSTALLED_FACTORIES[("zone_rest", selected)](configs=configs)


def create_zone_hygiene_formula(configs: dict[str, dict[str, Any]]) -> ZoneEnvironmentFormula:
    selected = configs.get("policy_registry", {}).get("zone_hygiene", "zone-hygiene-linear-v1")
    return _INSTALLED_FACTORIES[("zone_hygiene", selected)](configs=configs)


def create_zone_privacy_formula(configs: dict[str, dict[str, Any]]) -> ZoneEnvironmentFormula:
    selected = configs.get("policy_registry", {}).get("zone_privacy", "zone-privacy-threshold-v1")
    return _INSTALLED_FACTORIES[("zone_privacy", selected)](configs=configs)


def create_behavior(configs: dict[str, dict[str, Any]], **parameters: Any) -> BehaviorPolicy:
    if ("behavior", selected_models(configs)["behavior"]) in _INSTALLED_FACTORIES:
        return create_installed_model("behavior", configs, **parameters)
    return create_behavior_policy(selected_models(configs)["behavior"], **parameters)


def create_psychology(
    configs: dict[str, dict[str, Any]], *, resource_service_enabled: bool = False
) -> PsychologyPolicy:
    selected = selected_models(configs)["psychology"]
    if ("psychology", selected) in _INSTALLED_FACTORIES:
        return create_installed_model(
            "psychology", configs, resource_service_enabled=resource_service_enabled
        )
    config = PsychologyModelConfig.model_validate(configs.get("psychology", {}))
    values = config.coefficient_values()
    if resource_service_enabled:
        values["hydration_growth"] = configs["resource_service"].get(
            "hydration_growth_per_minute", 0.0
        )
    parameters = PsychologyParameters(**values)
    if selected == "mean-reverting-distress-v1":
        return MeanRevertingDistressPolicy(parameters)
    if selected == INTEGRATED_PSYCHOLOGY_POLICY_VERSION:
        return IntegratedDistressLifeStatePolicy(
            parameters,
            body_config=configs.get("body_dynamics"),
            body_formula=create_body_dynamics_formula(configs),
        )
    if selected == "life-state-feedback-v1":
        return LifeStatePsychologyPolicy(
            parameters,
            body_config=configs.get("body_dynamics"),
            body_formula=create_body_dynamics_formula(configs),
        )
    factory = {
        "causal-stress-v1": SimplePsychologyPolicy,
        "bounded-stress-v1": BoundedStressPolicy,
    }[selected]
    return factory(
        parameters,
        source_id=config.source_id if selected == "causal-stress-v1" else "ASM-BOUNDED-STRESS-V1",
    )


def create_social(configs: dict[str, dict[str, Any]]) -> StressPropagationPolicy:
    selected = selected_models(configs)["social"]
    if ("social", selected) in _INSTALLED_FACTORIES:
        return create_installed_model("social", configs)
    factories = {
        "sparse-family-zone-contact-v1": StressPropagationPolicy,
        "degroot-contact-v1": DeGrootContactPolicy,
        "expressiveness-channel-openness-v1": ExpressivenessChannelOpennessPolicy,
    }
    expected_source = MODEL_DEFINITIONS[selected]["assumption_source_id"]
    parameters = SocialPropagationParameters.from_mapping(
        configs.get("social_propagation"), source_id=expected_source
    )
    if selected_models(configs)["psychology"] in {
        "mean-reverting-distress-v1",
        INTEGRATED_PSYCHOLOGY_POLICY_VERSION,
    }:
        parameters = replace(parameters, natural_recovery_per_minute=0.0)
    return factories[selected](parameters)


def create_information(configs: dict[str, dict[str, Any]]) -> InformationSpreadPolicy | None:
    selected = selected_models(configs)["information"]
    if ("information", selected) in _INSTALLED_FACTORIES:
        return create_installed_model("information", configs)
    config = InformationSpreadConfig.from_mapping(configs.get("information_spread"))
    if not config.enabled:
        return None
    factory = {
        "sparse-information-contact-v1": InformationSpreadPolicy,
        "hazard-information-v1": HazardInformationPolicy,
        "evidence-aware-rumor-v1": EvidenceAwareInformationPolicy,
    }[selected]
    return factory(config)


def create_affect(configs: dict[str, dict[str, Any]]) -> Any:
    """Resolve the allowlisted affect operator selected in body_dynamics config."""
    from sheltersim.domain.body_state import canonical_body_config
    from sheltersim.policies.affect import create_affect_policy

    selected = selected_models(configs).get("affect")
    if selected is not None:
        return create_affect_policy(selected)
    config = canonical_body_config(configs.get("body_dynamics"))
    return create_affect_policy(config["affect_policy"])


def agents_social_model_catalog() -> dict[str, dict[str, Any]]:
    """Catalog formulas added by the autonomous agent/social model package."""
    from sheltersim.policies.agents_social_manifest import agents_social_model_catalog as catalog

    return catalog()


def register_agents_social_policies() -> None:
    """Install agent/social formulas in the shared registry during explicit app bootstrap.

    The bootstrap installs affect, relationship and physical queue policies. Importing
    the package never mutates registries; scenarios select installed models explicitly.
    """
    from sheltersim.policies.affect import affect_policy_catalog, create_affect_policy
    from sheltersim.policies.queueing import (
        NeedWeightedPhysicalQueuePolicy,
        StrictFIFOQueuePolicy,
    )
    from sheltersim.policies.relationships import (
        DistanceDurationRepeatTrustPolicy,
        DurationOnlyTrustPolicy,
    )

    for model_id, metadata in affect_policy_catalog().items():
        parameters = {
            "body_dynamics.affect_transfer_rate": {
                "unit": "fraction_per_recorded_contact",
                "lower": 0.0,
                "upper": 1.0,
                "default": 0.05,
            },
            "body_dynamics.social_action_affect_transfer": {
                "unit": "normalized_affect_per_social_event",
                "lower": 0.0,
                "upper": 1.0,
                "default": 0.02,
            },
        }
        definition = {
            "domain": "affect",
            "reference": metadata["reference"],
            "formula": metadata["formula"],
            "assumption_source_id": metadata["source_id"],
            "implementation_version": metadata["implementation_version"],
            "state_schema_version": 1,
            "parameters": parameters,
            "external_validation": False,
        }

        def factory(
            *, configs: dict[str, dict[str, Any]], _model_id: str = model_id, **kwargs: Any
        ) -> Any:
            del configs, kwargs
            return create_affect_policy(_model_id)

        if model_id in MODEL_DEFINITIONS:
            if MODEL_DEFINITIONS[model_id] != definition or not has_installed_factory(
                "affect", model_id
            ):
                raise ValueError("bundled affect model already registered differently")
        else:
            register_policy(model_id, definition, factory)

    relationship_models = {
        DistanceDurationRepeatTrustPolicy.model_id: DistanceDurationRepeatTrustPolicy,
        DurationOnlyTrustPolicy.model_id: DurationOnlyTrustPolicy,
    }
    for model_id, policy_type in relationship_models.items():
        source_id = policy_type.source_id
        parameters = {
            "body_dynamics.relationship_trust_gain": {
                "unit": "normalized_trust_per_recorded_contact",
                "default": 0.0005,
                "lower": 0.0,
                "upper": 1.0,
            },
            "body_dynamics.relationship_trust_decay_per_day": {
                "unit": "normalized_trust_per_day",
                "default": 0.002,
                "lower": 0.0,
                "upper": 1.0,
            },
            "body_dynamics.social_interaction_minutes": {
                "unit": "minute_per_interaction_scale",
                "default": 5,
                "lower": 1,
                "upper": 1440,
            },
        }
        if model_id == DistanceDurationRepeatTrustPolicy.model_id:
            parameters.update(
                {
                    "body_dynamics.relationship_distance_scale_m": {
                        "unit": "metre",
                        "default": 2.0,
                        "lower": 0.1,
                        "upper": 100.0,
                    },
                    "body_dynamics.relationship_repeat_contact_bonus": {
                        "unit": "relative_trust_gain_per_prior_contact",
                        "default": 0.05,
                        "lower": 0.0,
                        "upper": 1.0,
                    },
                }
            )
        definition = {
            "domain": "relationship",
            "reference": "Assumption; no shelter contact-distance or trust calibration supplied",
            "formula": policy_type.formula,
            "assumption_source_id": source_id,
            "implementation_version": "1",
            "state_schema_version": 1,
            "external_validation": False,
            "parameters": parameters,
        }

        def relationship_factory(
            *, configs: dict[str, dict[str, Any]], _policy_type: Any = policy_type, **kwargs: Any
        ) -> Any:
            del kwargs
            if _policy_type is DistanceDurationRepeatTrustPolicy:
                body_config = configs.get("body_dynamics", {})
                return _policy_type(
                    distance_scale_m=float(body_config.get("relationship_distance_scale_m", 2.0)),
                    repeat_contact_bonus=float(
                        body_config.get("relationship_repeat_contact_bonus", 0.05)
                    ),
                )
            return _policy_type()

        if model_id in MODEL_DEFINITIONS:
            if MODEL_DEFINITIONS[model_id] != definition or not has_installed_factory(
                "relationship", model_id
            ):
                raise ValueError("bundled relationship model already registered differently")
        else:
            register_policy(model_id, definition, relationship_factory)

    queue_models = {
        NeedWeightedPhysicalQueuePolicy.model_id: (
            NeedWeightedPhysicalQueuePolicy,
            "ASM-NEED-WEIGHTED-PHYSICAL-QUEUE-V1",
        ),
        StrictFIFOQueuePolicy.model_id: (
            StrictFIFOQueuePolicy,
            "ASM-STRICT-FIFO-QUEUE-V1",
        ),
    }
    for model_id, (policy_type, source_id) in queue_models.items():
        if policy_type is NeedWeightedPhysicalQueuePolicy:
            queue_parameters = {
                "facility_queues.abandon_after_minutes_by_service.*": {
                    "unit": "minute",
                    "default": {"toilet": 120, "water": 240, "meal": 360},
                    "lower": 1,
                    "upper": 10080,
                    "scope": "per_service",
                },
                "facility_queues.retry_delay_minutes": {
                    "unit": "minute",
                    "default": 15,
                    "lower": 1,
                    "upper": 1440,
                },
                "facility_queues.persistence_urgency_threshold": {
                    "unit": "normalized_0_1",
                    "default": 0.8,
                    "lower": 0.0,
                    "upper": 1.0,
                },
            }
        else:
            # StrictFIFOQueuePolicy ignores every facility_queues tuning field.
            # Do not advertise non-consumed values as replaceable coefficients.
            queue_parameters = {}
        definition = {
            "domain": "queue",
            "reference": "Assumption; no shelter queue calibration supplied",
            "formula": policy_type.formula,
            "assumption_source_id": source_id,
            "implementation_version": "1",
            "state_schema_version": 1,
            "external_validation": False,
            "parameters": queue_parameters,
        }

        def factory(
            *, configs: dict[str, dict[str, Any]], _policy_type: Any = policy_type, **kwargs: Any
        ) -> Any:
            del kwargs
            return _policy_type(configs.get("facility_queues", {}))

        if model_id in MODEL_DEFINITIONS:
            if MODEL_DEFINITIONS[model_id] != definition or not has_installed_factory(
                "queue", model_id
            ):
                raise ValueError("bundled queue model already registered differently")
        else:
            register_policy(model_id, definition, factory)

    from sheltersim.domain.social_network import (
        PROXIMITY_CONTACT_SOURCE_ID,
    )
    from sheltersim.policies.proximity import (
        DISABLED_PROXIMITY_POLICY_ID,
        DISTANCE_PROXIMITY_POLICY_ID,
        create_proximity_policy,
    )

    proximity_models = {
        DISABLED_PROXIMITY_POLICY_ID: {
            "reference": "Explicitly disabled; no proximity events are generated",
            "formula": "no sampled contacts",
            "assumption_source_id": "ASM-NO-SOCIAL-PROXIMITY-V1",
            "parameters": {},
        },
        DISTANCE_PROXIMITY_POLICY_ID: {
            "reference": "Assumption; no shelter contact-distance or proximity-frequency calibration supplied",
            "formula": "physical same-zone positions; Euclidean distance <= max_distance_m; nearest pairs then stable Agent IDs; bounded pairs per sample",
            "assumption_source_id": PROXIMITY_CONTACT_SOURCE_ID,
            "parameters": {
                "social_proximity.sample_interval_minutes": {
                    "unit": "minute",
                    "default": 15,
                    "lower": 1,
                    "upper": 60,
                    "source_id": PROXIMITY_CONTACT_SOURCE_ID,
                    "evidence_grade": "C",
                    "applicability": "Synthetic sampling assumption; no observed shelter contact-frequency calibration",
                },
                "social_proximity.max_distance_m": {
                    "unit": "metre",
                    "default": 1.5,
                    "lower": 0.25,
                    "upper": 10.0,
                    "source_id": PROXIMITY_CONTACT_SOURCE_ID,
                    "evidence_grade": "C",
                    "applicability": "Synthetic proximity threshold; not a measured social-interaction boundary",
                },
                "social_proximity.max_pairs_per_sample": {
                    "unit": "pair_count_per_sample",
                    "default": 500,
                    "lower": 1,
                    "upper": 10_000,
                    "source_id": PROXIMITY_CONTACT_SOURCE_ID,
                    "evidence_grade": "C",
                    "applicability": "Computational cap only; does not imply observed-contact prevalence",
                },
            },
        },
    }
    for model_id, metadata in proximity_models.items():
        definition = {
            "domain": "social_proximity",
            **metadata,
            "implementation_version": "1",
            "state_schema_version": 1,
            "external_validation": False,
        }

        def proximity_factory(
            *, configs: dict[str, dict[str, Any]], _model_id: str = model_id, **kwargs: Any
        ) -> Any:
            del kwargs
            return create_proximity_policy(_model_id, configs)

        if model_id in MODEL_DEFINITIONS:
            if MODEL_DEFINITIONS[model_id] != definition or not has_installed_factory(
                "social_proximity", model_id
            ):
                raise ValueError("bundled social proximity model already registered differently")
        else:
            register_policy(model_id, definition, proximity_factory)


__all__ = [
    "agents_social_model_catalog",
    "create_affect",
    "register_agents_social_policies",
    "create_behavior",
    "create_psychology",
    "create_social",
    "create_information",
    "model_catalog",
    "model_signatures",
]
