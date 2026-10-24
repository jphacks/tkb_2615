"""Defaults for newly authored Scenarios; saved explicit selectors remain intact."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sheltersim.contracts.policies import CORE_MODEL_SUITE_MODELS, CORE_MODEL_SUITE_V2
from sheltersim.domain.body_state import canonical_body_config
from sheltersim.domain.models import InformationSpreadConfig
from sheltersim.experiments.scenario import PsychologyModelConfig, ScenarioSpec
from sheltersim.policies.social import SocialPropagationParameters

CORE_MODEL_SUITE_SOURCE_ID = "ASM-CORE-MODEL-SUITE-V2"
SLEEP_DEBT_BALANCE_SOURCE_ID = "ASM-SLEEP-DEBT-BALANCE-V1"


def install_default_model_suite(
    configs: dict[str, dict[str, Any]], *, force: bool = False
) -> dict[str, dict[str, Any]]:
    """Return new-run defaults only when no explicit Policy Registry is present.

    Historical Scenarios with a registry selector keep their exact model set.
    New, unselected Scenario inputs receive a pinned candidate suite and coefficient set.
    """
    result = deepcopy(configs)
    if "policy_registry" in result and not force:
        return result

    behavior = result.setdefault("behavior", {})
    behavior.setdefault("policy", CORE_MODEL_SUITE_MODELS["behavior"])
    if force:
        behavior["decision_interval_minutes"] = 15
    else:
        behavior.setdefault("decision_interval_minutes", 15)
    behavior.setdefault("temperature", 0.2)
    behavior.setdefault("source_type", "assumption")
    behavior.setdefault(
        "source_id",
        "ASM-PRIORITY-RULE-BEHAVIOR-V1"
        if behavior["policy"] == "priority-rule-v1"
        else "ASM-SLEEP-AWARE-BEHAVIOR-V2",
    )
    if behavior["policy"] not in {
        "utility-softmax-v1",
        CORE_MODEL_SUITE_MODELS["behavior"],
    }:
        # A behavior variant explicitly stored without a suite remains an
        # explicit research/comparison input rather than an accidental mix.
        return result
    behavior["policy"] = CORE_MODEL_SUITE_MODELS["behavior"]
    behavior["source_id"] = "ASM-SLEEP-AWARE-BEHAVIOR-V2"
    behavior.setdefault("sleep_debt_utility_weight", 1.0)

    result.setdefault("psychology", PsychologyModelConfig().to_model_config())
    body = canonical_body_config(result.get("body_dynamics"))
    if force:
        body["affect_policy"] = CORE_MODEL_SUITE_MODELS["affect"]
    elif body["affect_policy"] != CORE_MODEL_SUITE_MODELS["affect"]:
        return result
    if force:
        body["awake_sleep_debt_minutes_per_minute"] = 0.5
        body["rest_sleep_debt_recovery_per_minute"] = 0.0
    else:
        raw_body = result.get("body_dynamics", {})
        if "awake_sleep_debt_minutes_per_minute" not in raw_body:
            body["awake_sleep_debt_minutes_per_minute"] = 0.5
        if "rest_sleep_debt_recovery_per_minute" not in raw_body:
            body["rest_sleep_debt_recovery_per_minute"] = 0.0
    result["body_dynamics"] = body

    social = result.setdefault(
        "social_propagation",
        {
            **{
                **SocialPropagationParameters().provenance()["coefficients"],
                "source_id": "ASM-BOSSE-AFFECT-CONTAGION-V1",
            },
            "enabled": True,
            "source_type": "assumption",
            "source_id": "ASM-BOSSE-AFFECT-CONTAGION-V1",
        },
    )
    social.setdefault("enabled", True)
    social.setdefault("source_type", "assumption")
    social.setdefault("source_id", "ASM-BOSSE-AFFECT-CONTAGION-V1")

    if "information_spread" not in result:
        information = InformationSpreadConfig().to_dict()
        information["enabled"] = True
        result["information_spread"] = information

    if force:
        # Physical proximity requires recorded geometry; a suite never invents it.
        result.pop("social_proximity", None)
    elif result.get("social_proximity", {}).get("enabled") is True:
        # An enabled proximity mechanism is an explicit input and needs a matching
        # model selector, so leave the Scenario outside the default suite.
        return result
    extras = {
        key: value
        for key, value in result.get("policy_registry", {}).items()
        if key not in {"schema_version", "suite_id", *CORE_MODEL_SUITE_MODELS}
    }
    result["policy_registry"] = {
        "schema_version": 3,
        "suite_id": CORE_MODEL_SUITE_V2,
        **extras,
    }
    return result


def apply_default_model_suite(scenario: ScenarioSpec, *, force: bool = False) -> ScenarioSpec:
    """Pin the internal core suite on an unselected newly authored Scenario."""
    configs = install_default_model_suite(scenario.model_configs, force=force)
    if configs == scenario.model_configs:
        return scenario
    document = scenario.model_dump(mode="json")
    document["model_configs"] = configs
    provenance = document.setdefault("provenance", {})
    sources = provenance.setdefault("sources", [])
    if not any(item.get("id") == CORE_MODEL_SUITE_SOURCE_ID for item in sources):
        sources.append(
            {
                "id": CORE_MODEL_SUITE_SOURCE_ID,
                "source_kind": "assumption",
                "title": CORE_MODEL_SUITE_V2,
                "description": (
                    "Versioned internal model combination. Operators and coefficients are "
                    "synthetic assumptions; this is not external validation."
                ),
            }
        )
    assumptions = provenance.setdefault("model_assumption_ids", [])
    managed_assumptions = {
        "ASM-CORE-MODEL-SUITE-V1",
        "ASM-CORE-MODEL-SUITE-V2",
        "ASM-UTILITY-BEHAVIOR-V1",
        "ASM-SLEEP-AWARE-BEHAVIOR-V2",
        "ASM-PRIORITY-RULE-BEHAVIOR-V1",
        SLEEP_DEBT_BALANCE_SOURCE_ID,
    }
    assumptions[:] = [item for item in assumptions if item not in managed_assumptions]
    behavior_source = configs["behavior"].get("source_id")
    if behavior_source and behavior_source not in assumptions:
        assumptions.append(behavior_source)
    for source_id in (CORE_MODEL_SUITE_SOURCE_ID, SLEEP_DEBT_BALANCE_SOURCE_ID):
        if source_id not in assumptions:
            assumptions.append(source_id)
    return ScenarioSpec.model_validate(document)
