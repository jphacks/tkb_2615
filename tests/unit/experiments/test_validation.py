"""Scientific-comparison checks prepared for final #122 execution."""

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.validation import (
    EvaluationVariant,
    _variant_scenario,
    check_dataset,
    evidence_context,
    paired_summary,
    parameter_catalog,
)


def row(seed, value, input_hash="same"):
    return {"seed": seed, "input_hash": input_hash, "metrics": {"stress_mean": value}}


def test_single_seed_has_no_confidence_interval():
    summary = paired_summary([row(1, 0.4)], [row(1, 0.3)], "stress_mean")
    assert summary["uncertain"] is True
    assert summary["approximate_95_percent_seed_mean_interval"] is None


def test_paired_inputs_and_seeds_are_required():
    with pytest.raises(ValueError, match="inputs"):
        paired_summary([row(1, 0.4)], [row(1, 0.3, "changed")], "stress_mean")
    with pytest.raises(ValueError, match="duplicate"):
        paired_summary([row(1, 0.4), row(1, 0.4)], [row(1, 0.3)], "stress_mean")


def test_empirical_dataset_cannot_apply_to_unmatched_region():
    scenario = ScenarioSpec.load(SCENARIO_100_AGENTS)
    dataset = {
        "id": "region-specific",
        "applicability": {"municipality_code": "13107"},
        "metric": "stress_mean",
    }
    assert check_dataset(dataset, scenario, {"stress_mean": 0.5})["status"] == "not_applicable"


def test_evaluation_variant_applies_registered_task_duration_map(generated_scenario):
    base = generated_scenario
    path = "staff_operations.task_duration_minutes.*"
    entry = next(row for row in parameter_catalog(base) if row["path"] == path)
    assert entry["scope"] == "per_task_kind"
    assert entry["value"]["cleaning"] == 15
    zone_path = "staff_operations.effects_by_zone.*"
    zone_entry = next(row for row in parameter_catalog(base) if row["path"] == zone_path)
    assert zone_entry["scope"] == "per_zone_id"

    variant = EvaluationVariant.model_validate(
        {
            "id": "short-cleaning",
            "parameters": {
                path: {"cleaning": 9},
                zone_path: {"sleeping": {"cleaning_sanitation_recovery": 0.3}},
            },
        }
    )
    changed = _variant_scenario(base, variant, seed=8)

    assert changed.seed == 8
    assert changed.model_configs["staff_operations"]["task_duration_minutes"] == {
        **base.model_configs["staff_operations"]["task_duration_minutes"],
        "cleaning": 9,
    }
    assert changed.provenance["assumptions"][-1]["scope"] == "per_task_kind"
    assert changed.provenance["assumptions"][-1]["value"] == {"cleaning": 9}
    assert changed.model_configs["staff_operations"]["effects_by_zone"]["sleeping"] == {
        "cleaning_sanitation_recovery": 0.3,
    }
    assert any(
        row.get("path") == zone_path
        and row.get("value") == {"sleeping": {"cleaning_sanitation_recovery": 0.3}}
        for row in changed.provenance["assumptions"]
    )
    with pytest.raises(ValueError, match="finite numbers"):
        EvaluationVariant.model_validate(
            {
                "id": "non-finite",
                "parameters": {path: {"cleaning": float("nan")}},
            }
        )


def test_legacy_psychology_variant_removes_incompatible_inherited_formula_selectors(
    generated_scenario,
):
    base = generated_scenario
    changed = _variant_scenario(
        base,
        EvaluationVariant.model_validate(
            {
                "id": "legacy-psychology",
                "policies": {"psychology": "causal-stress-v1"},
            }
        ),
        seed=8,
    )

    assert changed.model_configs["policy_registry"]["psychology"] == "causal-stress-v1"
    assert "body_dynamics" not in changed.model_configs
    assert not {"body_dynamics", "care_outcome", "affect"}.intersection(
        changed.model_configs["policy_registry"]
    )


def test_generated_scenario_evidence_is_safe_json_for_assumption_maps(generated_scenario):
    base = generated_scenario

    evidence = evidence_context(base)
    household_assumption = next(
        item
        for item in evidence["scenario_assumptions"]
        if item.get("subject") == "synthetic_household_size_mix"
    )
    assert household_assumption["value"] == {
        "1": 0.2,
        "2": 0.25,
        "3": 0.25,
        "4": 0.2,
        "5": 0.1,
    }


def test_evaluation_variant_applies_resident_specific_need_growth_map(generated_scenario):
    base = generated_scenario
    path = "psychology.toilet_growth_by_agent.*"
    agent_id = base.profiles[0].id
    entry = next(row for row in parameter_catalog(base) if row["path"] == path)
    assert entry["scope"] == "per_agent_id"
    assert entry["unit"] == "normalized_per_minute"
    assert entry["value"] == {}
    assert entry["source_id"] == "ASM-LIFE-STATE-FEEDBACK-V1"
    assert entry["evidence_grade"] == "C"
    assert "unseen future arrivals use the shared rate" in entry["applicability"]

    variant = EvaluationVariant.model_validate(
        {
            "id": "resident-toilet-rate",
            "parameters": {path: {agent_id: 0.02}},
        }
    )
    changed = _variant_scenario(base, variant, seed=8)

    assert changed.seed == 8
    assert changed.model_configs["psychology"]["toilet_growth_by_agent"] == {
        agent_id: 0.02,
    }
    assert changed.provenance["assumptions"][-1]["scope"] == "per_agent_id"
    assert changed.provenance["assumptions"][-1]["value"] == {agent_id: 0.02}


def test_evaluation_variant_applies_resident_specific_behavior_cadence(generated_scenario):
    base = generated_scenario
    agent_id = base.profiles[0].id
    path = "behavior.decision_interval_minutes_by_agent.*"
    entry = next(row for row in parameter_catalog(base) if row["path"] == path)
    assert entry["scope"] == "per_agent_id"
    assert entry["unit"] == "minute_by_agent_id"
    assert entry["value"] == {}

    variant = EvaluationVariant.model_validate(
        {
            "id": "resident-cadence",
            "parameters": {path: {agent_id: 30}},
        }
    )
    changed = _variant_scenario(base, variant, seed=8)

    assert changed.seed == 8
    assert changed.model_configs["behavior"]["decision_interval_minutes_by_agent"] == {
        agent_id: 30,
    }
    assert changed.provenance["assumptions"][-1]["scope"] == "per_agent_id"
    assert changed.provenance["assumptions"][-1]["value"] == {agent_id: 30}


def test_evaluation_variant_applies_registered_mobility_speed(generated_scenario):
    base = generated_scenario
    path = "movement_model.speed_m_per_minute_by_mobility.assisted"
    entry = next(row for row in parameter_catalog(base) if row["path"] == path)
    assert entry["value"] == 36.0
    assert entry["unit"] == "metre_per_minute"
    assert entry["evidence_grade"] == "C"

    variant = EvaluationVariant.model_validate(
        {
            "id": "slower-assisted-movement",
            "parameters": {path: 18.0},
        }
    )
    changed = _variant_scenario(base, variant, seed=8)

    assert changed.seed == 8
    assert (
        changed.model_configs["movement_model"]["speed_m_per_minute_by_mobility"]["assisted"]
        == 18.0
    )
    applied = next(row for row in changed.provenance["assumptions"] if row.get("path") == path)
    assert applied["value"] == 18.0
    assert any(source["id"] == applied["source_id"] for source in changed.provenance["sources"])


def test_evaluation_variant_applies_resource_service_coefficients(generated_scenario_with):
    base = generated_scenario_with(
        operational_conditions={
            "initial_resources": {"status": "entered", "water_ml": 5_000, "food_meal_units": 20}
        }
    )
    path = "resource_service.hydration_relief"
    entry = next(row for row in parameter_catalog(base) if row["path"] == path)
    assert entry["value"] == 0.35
    assert entry["source_id"] == base.model_configs["resource_service"]["source_id"]
    assert entry["evidence_grade"] == "C"

    variant = EvaluationVariant.model_validate(
        {
            "id": "reduced-water-relief",
            "parameters": {path: 0.2},
        }
    )
    changed = _variant_scenario(base, variant, seed=8)

    assert changed.seed == 8
    assert changed.model_configs["resource_service"]["hydration_relief"] == 0.2
    applied = next(row for row in changed.provenance["assumptions"] if row.get("path") == path)
    assert applied["value"] == 0.2
    assert any(source["id"] == applied["source_id"] for source in changed.provenance["sources"])


def test_evaluation_variant_applies_toilet_and_staff_lifecycle_coefficients(
    generated_scenario_with,
):
    base = generated_scenario_with(
        operational_conditions={
            "staff_shifts": [{"staff_id": "staff_001", "start_minute": 0, "end_minute": 500}]
        }
    )
    parameters = {
        "toilet_service.minimum_minutes": 6,
        "toilet_service.maximum_minutes": 6,
        "staff_dynamics.rest_minutes": 45,
        "staff_dynamics.off_shift_fatigue_recovery_per_minute": 0.01,
        "staff.support_task_minutes": 12,
        "staff.unresolved_after_minutes": 120,
        "staff.support_cooldown_minutes": 40,
        "staff.support_fairness_minutes": 60,
    }
    rows = {row["path"]: row for row in parameter_catalog(base)}
    assert all(path in rows for path in parameters)
    assert (
        rows["toilet_service.minimum_minutes"]["source_id"]
        == (base.model_configs["toilet_service"]["source_id"])
    )
    assert (
        rows["staff_dynamics.rest_minutes"]["source_id"]
        == (base.model_configs["staff_dynamics"]["source_id"])
    )
    assert (
        rows["staff.support_task_minutes"]["source_id"]
        == (base.model_configs["staff"]["source_id"])
    )

    changed = _variant_scenario(
        base,
        EvaluationVariant.model_validate(
            {
                "id": "revised-service-and-shifts",
                "parameters": parameters,
            }
        ),
        seed=8,
    )
    assert changed.seed == 8
    assert changed.model_configs["toilet_service"]["minimum_minutes"] == 6
    assert changed.model_configs["toilet_service"]["maximum_minutes"] == 6
    assert changed.model_configs["staff_dynamics"]["rest_minutes"] == 45
    assert changed.model_configs["staff_dynamics"]["off_shift_fatigue_recovery_per_minute"] == 0.01
    assert changed.model_configs["staff"]["support_task_minutes"] == 12
    assert changed.model_configs["staff"]["unresolved_after_minutes"] == 120
    assert changed.model_configs["staff"]["support_cooldown_minutes"] == 40
    assert changed.model_configs["staff"]["support_fairness_minutes"] == 60
    assumptions = {
        row.get("path"): row
        for row in changed.provenance["assumptions"]
        if row.get("path") in parameters
    }
    assert set(assumptions) == set(parameters)
    assert all(assumptions[path]["source_id"] == rows[path]["source_id"] for path in parameters)
    source_ids = {source["id"] for source in changed.provenance["sources"]}
    assert {assumptions[path]["source_id"] for path in parameters} <= source_ids


def test_calibration_candidate_uses_training_score_and_reports_holdout(tmp_path):
    from sheltersim.storage.experiments import ExperimentStore
    from sheltersim.storage.projects import ProjectStore, digest

    store = ProjectStore(tmp_path)
    store.create_project("p")
    experiments = ExperimentStore(store)
    root = experiments._path("p", "calibration")
    root.mkdir(parents=True)
    body = {
        "schema_version": 1,
        "experiment_id": "calibration",
        "purpose": "calibration",
        "observations": [
            {
                "dataset_id": "train",
                "metric": "stress_mean",
                "value": 0,
                "scale": 1,
                "weight": 1,
                "role": "training",
            },
            {
                "dataset_id": "holdout",
                "metric": "stress_mean",
                "value": 100,
                "scale": 1,
                "weight": 1,
                "role": "holdout",
            },
        ],
        "dataset_checks": [
            {"dataset_id": "train", "applicable": True},
            {"dataset_id": "holdout", "applicable": True},
        ],
        "plan": {
            "variants": [
                {"id": "train-best", "parameters": {}, "policies": {}},
                {"id": "holdout-best", "parameters": {}, "policies": {}},
            ]
        },
        "cells": [
            {"cell_id": "cell-1", "variant_id": "train-best"},
            {"cell_id": "cell-2", "variant_id": "holdout-best"},
        ],
    }
    record = {**body, "plan_hash": digest(body)}
    with store.transaction():
        store._write(
            experiments._path("p", "calibration", "experiment.json"), record, immutable=True
        )
        store._write(
            experiments._path("p", "calibration", "results", "cell-1.json"),
            {"schema_version": 1, "metrics": {"stress_mean": 1.0}},
            immutable=True,
        )
        store._write(
            experiments._path("p", "calibration", "results", "cell-2.json"),
            {"schema_version": 1, "metrics": {"stress_mean": 2.0}},
            immutable=True,
        )

    result = experiments.fit("p", "calibration")

    assert result["candidate"]["variant_id"] == "train-best"
    assert result["candidate"]["training_score"] == 1.0
    assert result["candidate"]["holdout_score"] == 9801.0
    holdout_candidate = next(row for row in result["scores"] if row["variant_id"] == "holdout-best")
    assert holdout_candidate["holdout_score"] == 9604.0
