"""Units and evidence status for exchangeable agent/social model assumptions.

This catalog describes the model factories implemented in this worker. Every listed
parameter remains a scenario/model assumption until an evidence-backed replacement
is registered in the shared Policy Registry.
"""

from __future__ import annotations

from typing import Any

_BODY_UNITS = {
    "awake_sleep_debt_minutes_per_minute": "minute_sleep_debt_per_minute_awake",
    "rest_sleep_debt_recovery_per_minute": "minute_sleep_debt_per_minute_resting",
    "sleep_sleep_debt_recovery_per_minute": "minute_sleep_debt_per_minute_sleeping",
    "sleep_debt_stress_rate": "normalized_stress_per_sleep_debt_minute_per_minute",
    "uncertainty_drift_per_minute": "normalized_uncertainty_per_minute",
    "uncertainty_decay_per_minute": "normalized_uncertainty_per_minute",
    "discontent_stress_rate": "normalized_stress_per_discontent_per_minute",
    "discontent_queue_rate": "normalized_discontent_per_queue_minute",
    "discontent_relief_per_minute": "normalized_discontent_per_minute",
    "reassurance_decay_per_minute": "normalized_reassurance_per_minute",
    "positive_affect_decay_per_minute": "normalized_affect_per_minute",
    "negative_affect_stress_rate": "normalized_stress_per_affect_per_minute",
    "hygiene_growth_per_minute": "normalized_hygiene_need_per_minute",
    "sanitation_hygiene_growth_per_minute": (
        "normalized_hygiene_need_per_minute_per_sanitation_deficit"
    ),
    "sanitation_stress_rate": "normalized_stress_per_sanitation_deficit_per_minute",
    "affect_transfer_rate": "normalized_affect_per_recorded_contact",
    "relationship_trust_gain": "normalized_trust_per_recorded_contact",
    "relationship_trust_decay_per_day": "normalized_trust_per_day",
    "relationship_repeat_contact_bonus": "relative_trust_gain_per_prior_contact",
    "cooperation_positive_gain": "normalized_affect_per_cooperation_event",
    "cooperation_negative_reduction": "normalized_affect_per_cooperation_event",
    "cooperation_reassurance_gain": "normalized_reassurance_per_cooperation_event",
    "cooperation_discontent_reduction": "normalized_discontent_per_cooperation_event",
    "social_action_affect_transfer": "normalized_affect_per_social_event",
    "complaint_reassurance_gain": "normalized_reassurance_per_staff_response",
    "complaint_discontent_reduction": "normalized_discontent_per_staff_response",
    "hygiene_support_relief": "normalized_hygiene_need_per_completed_staff_support",
    "caregiver_assistance_stress_relief": "normalized_stress_relief_per_caregiver_assistance",
    "caregiver_assistance_reassurance_gain": "normalized_reassurance_per_caregiver_assistance",
    "privacy_accommodation_discontent_relief": (
        "normalized_discontent_relief_per_privacy_accommodation"
    ),
    "mobility_assistance_stress_relief": "normalized_stress_relief_per_mobility_assistance",
    "language_access_uncertainty_relief": "normalized_uncertainty_relief_per_language_access",
    "medical_assessment_uncertainty_relief": "normalized_uncertainty_relief_per_medical_assessment",
    "medication_support_uncertainty_relief": "normalized_uncertainty_relief_per_medication_support",
    "dependent_assistance_stress_relief": "normalized_stress_relief_per_dependent_assistance",
    "dependent_assistance_reassurance_gain": "normalized_reassurance_per_dependent_assistance",
    "correction_uncertainty_reduction": "normalized_uncertainty_per_recorded_correction",
    "correction_reassurance_gain": "normalized_reassurance_per_recorded_correction",
    "social_interaction_minutes": "minute_per_recorded_social_interaction",
}
_BODY_BOUNDS = {
    name: [
        0.0,
        10.0
        if name
        in {
            "awake_sleep_debt_minutes_per_minute",
            "rest_sleep_debt_recovery_per_minute",
            "sleep_sleep_debt_recovery_per_minute",
        }
        else 1.0,
    ]
    for name in _BODY_UNITS
    if name != "social_interaction_minutes"
}
_BODY_BOUNDS["relationship_repeat_contact_bonus"] = [0.0, 1.0]

_MOVEMENT_UNITS = {
    "speed_m_per_minute_by_mobility.independent": "metre_per_minute",
    "speed_m_per_minute_by_mobility.assisted": "metre_per_minute",
    "speed_m_per_minute_by_mobility.wheelchair": "metre_per_minute",
    "density_slowdown_per_person_m2": "dimensionless_slowdown_per_person_per_square_metre",
    "maximum_density_slowdown": "dimensionless_speed_factor",
}

_STAFF_UNITS = {
    "max_continuous_work_minutes": "minute",
    "rest_minutes": "minute",
    "shift_windows_by_staff.*.start_minute": "minute_of_week",
    "shift_windows_by_staff.*.end_minute": "minute_of_week_exclusive",
    "off_shift_fatigue_recovery_per_minute": "normalized_fatigue_per_minute",
}

_OPERATIONS_UNITS = {
    "task_duration_minutes.*": "minute",
    "sanitation_use_per_person_minute": "normalized_sanitation_index_per_person_minute",
    "cleaning_sanitation_recovery": "normalized_sanitation_index_per_cleaning_task",
    "outreach_uncertainty_relief": "normalized_uncertainty_per_outreach_task",
    "patrol_reassurance": "normalized_reassurance_per_patrol_task",
}

_ENVIRONMENT_UNITS = {
    "noise_rest_efficiency_loss": "relative_rest_efficiency_per_normalized_noise",
    "min_probability_multiplier": "dimensionless_action_odds_multiplier",
    "max_probability_multiplier": "dimensionless_action_odds_multiplier",
    "min_rest_decision_minutes": "minute",
    "max_rest_decision_minutes": "minute",
    "max_active_modifiers_per_zone": "modifier_count",
    "lighting_sleep_efficiency_loss": "relative_sleep_efficiency_per_normalized_lighting",
    "ventilation_hygiene_growth_per_minute": (
        "normalized_hygiene_need_per_minute_per_ventilation_deficit"
    ),
    "privacy_discontent_per_minute": "normalized_discontent_per_minute_per_privacy_deficit",
    "temperature_stress_per_degree_minute": "normalized_stress_per_celsius_degree_per_minute",
    "comfortable_min_temperature_c": "degree_celsius",
    "comfortable_max_temperature_c": "degree_celsius",
    "baseline_by_zone.*.ventilation_effectiveness": "relative_ventilation_effectiveness_0_1",
    "baseline_by_zone.*.lighting_level": "normalized_lighting_0_1",
    "baseline_by_zone.*.privacy_level": "normalized_privacy_0_1",
    "baseline_by_zone.*.temperature_c": "degree_celsius",
}
_ENVIRONMENT_BOUNDS = {
    "noise_rest_efficiency_loss": [0.0, 0.9],
    "min_probability_multiplier": [0.1, 1.0],
    "max_probability_multiplier": [1.0, 2.0],
    "min_rest_decision_minutes": [1, 60],
    "max_rest_decision_minutes": [1, 240],
    "max_active_modifiers_per_zone": [1, 16],
    "lighting_sleep_efficiency_loss": [0.0, 0.9],
    "ventilation_hygiene_growth_per_minute": [0.0, 0.01],
    "privacy_discontent_per_minute": [0.0, 0.01],
    "temperature_stress_per_degree_minute": [0.0, 0.01],
    "comfortable_min_temperature_c": [-10.0, 40.0],
    "comfortable_max_temperature_c": [-10.0, 50.0],
}


def agents_social_model_catalog() -> dict[str, dict[str, Any]]:
    """Return installed formulas with defaults, versions, units, and assumption status."""
    from sheltersim.domain.body_state import _DEFAULTS as body_defaults
    from sheltersim.domain.environment import ScenarioZoneEnvironmentConfig
    from sheltersim.domain.movement import canonical_movement_config
    from sheltersim.domain.staff_dynamics import canonical_staff_dynamics_config
    from sheltersim.domain.staff_operations import canonical_staff_operations_config
    from sheltersim.policies.affect import affect_policy_catalog

    body = {
        name: _parameter(value, _BODY_UNITS[name], bounds=_BODY_BOUNDS[name])
        for name, value in body_defaults.items()
    }
    body["social_interaction_minutes"] = _parameter(
        5, _BODY_UNITS["social_interaction_minutes"], bounds=[1, 1440]
    )
    body["sleep_debt_stress_rate_by_agent.*"] = _parameter(
        {},
        "normalized_stress_per_minute_by_agent_id",
        bounds=[0.0, 1.0],
        scope="per_agent_id",
        replaceable=True,
        applicability=(
            "Optional experimental per-resident coefficient for explicit Agent IDs; "
            "other residents use the shared sleep-debt rate"
        ),
    )
    body["complaint_reassurance_gain_by_agent.*"] = _parameter(
        {},
        "normalized_reassurance_per_staff_response_by_agent_id",
        bounds=[0.0, 1.0],
        scope="per_agent_id",
        replaceable=True,
        applicability=(
            "Optional experimental per-resident emotional-support response for explicit Agent IDs; "
            "other residents use the shared reassurance gain"
        ),
    )
    per_agent_parameters = {
        "sleep_debt_stress_rate_by_agent": (
            "normalized_stress_per_sleep_debt_minute_per_minute_by_agent_id",
            "Residents with an explicit ID may use a separate sleep-debt response; others "
            "use the shared coefficient",
        ),
        "negative_affect_stress_rate_by_agent": (
            "normalized_stress_per_negative_affect_per_minute_by_agent_id",
            "Residents with an explicit ID may use a separate negative-affect response; "
            "others use the shared coefficient",
        ),
        "discontent_stress_rate_by_agent": (
            "normalized_stress_per_discontent_per_minute_by_agent_id",
            "Residents with an explicit ID may use a separate discontent response; others "
            "use the shared coefficient",
        ),
        "complaint_reassurance_gain_by_agent": (
            "normalized_reassurance_per_staff_response_by_agent_id",
            "Residents with an explicit ID may use a separate staff reassurance response; "
            "others use the shared coefficient",
        ),
        "hygiene_support_relief_by_agent": (
            "normalized_hygiene_need_per_completed_staff_support_by_agent_id",
            "Residents with an explicit ID may use a separate hygiene-support response; "
            "others use the shared coefficient",
        ),
        "caregiver_assistance_stress_relief_by_agent": (
            "normalized_stress_relief_per_caregiver_assistance_by_agent_id",
            "Residents with an explicit ID may use a separate caregiver stress response; "
            "others use the shared coefficient",
        ),
        "caregiver_assistance_reassurance_gain_by_agent": (
            "normalized_reassurance_per_caregiver_assistance_by_agent_id",
            "Residents with an explicit ID may use a separate caregiver reassurance "
            "response; others use the shared coefficient",
        ),
        "privacy_accommodation_discontent_relief_by_agent": (
            "normalized_discontent_relief_per_privacy_accommodation_by_agent_id",
            "Residents with an explicit ID may use a separate privacy response; others use "
            "the shared coefficient",
        ),
        "mobility_assistance_stress_relief_by_agent": (
            "normalized_stress_relief_per_mobility_assistance_by_agent_id",
            "Residents with an explicit ID may use a separate mobility response; others use "
            "the shared coefficient",
        ),
        "language_access_uncertainty_relief_by_agent": (
            "normalized_uncertainty_relief_per_language_access_by_agent_id",
            "Residents with an explicit ID may use a separate language-access response; "
            "others use the shared coefficient",
        ),
        "medical_assessment_uncertainty_relief_by_agent": (
            "normalized_uncertainty_relief_per_medical_assessment_by_agent_id",
            "Residents with an explicit ID may use a separate medical-assessment response; "
            "others use the shared coefficient",
        ),
        "medication_support_uncertainty_relief_by_agent": (
            "normalized_uncertainty_relief_per_medication_support_by_agent_id",
            "Residents with an explicit ID may use a separate medication-support response; "
            "others use the shared coefficient",
        ),
        "dependent_assistance_stress_relief_by_agent": (
            "normalized_stress_relief_per_dependent_assistance_by_agent_id",
            "Residents with an explicit ID may use a separate dependent stress response; "
            "others use the shared coefficient",
        ),
        "dependent_assistance_reassurance_gain_by_agent": (
            "normalized_reassurance_per_dependent_assistance_by_agent_id",
            "Residents with an explicit ID may use a separate dependent reassurance "
            "response; others use the shared coefficient",
        ),
    }
    for field, (unit, applicability) in per_agent_parameters.items():
        body[f"{field}.*"] = _parameter(
            {},
            unit,
            bounds=[0.0, 1.0],
            scope="per_agent_id",
            replaceable=True,
            applicability=applicability,
        )
    body["relationship_distance_scale_m"] = _parameter(2.0, "metre", bounds=[0.1, 100.0])
    body["affect_policy"] = _parameter(
        "linear-positive-negative-v1", "installed_policy_id", replaceable=True
    )
    movement_config = canonical_movement_config(None)
    movement = {
        name: _parameter(value, _MOVEMENT_UNITS[name], bounds=[0.0, None])
        for name, value in (
            (f"speed_m_per_minute_by_mobility.{mobility}", speed)
            for mobility, speed in movement_config["speed_m_per_minute_by_mobility"].items()
        )
    }
    movement.update(
        {
            name: _parameter(
                movement_config[name],
                _MOVEMENT_UNITS[name],
                bounds=[1.0, None] if name == "maximum_density_slowdown" else [0.0, None],
            )
            for name in ("density_slowdown_per_person_m2", "maximum_density_slowdown")
        }
    )
    staff = canonical_staff_dynamics_config(None)
    staff_v2 = canonical_staff_dynamics_config(
        {
            "policy_version": "continuous-duty-fatigue-v2",
            "source_id": "ASM-STAFF-DYNAMICS-V2",
        }
    )
    staff_bounds = {
        "max_continuous_work_minutes": [1, 1440],
        "rest_minutes": [1, 480],
        "off_shift_fatigue_recovery_per_minute": [0.0, 1.0],
    }
    staff_parameters = {
        name: _parameter(staff[name], _STAFF_UNITS[name], bounds=staff_bounds[name])
        for name in staff_bounds
    }
    staff_v2_parameters = {
        name: _parameter(staff_v2[name], _STAFF_UNITS[name], bounds=staff_bounds[name])
        for name in staff_bounds
    }
    staff_v2_parameters["shift_windows_by_staff.*"] = _parameter(
        {},
        "minute_of_week_intervals",
        bounds=[0, 10080],
        lower=0,
        upper=10080,
        scope="per_staff_shift_windows",
        replaceable=True,
        applicability=(
            "Weekly [start,end) on-duty intervals for explicitly named staff IDs; "
            "intervals repeat every week and do not represent measured availability."
        ),
        max_intervals_per_staff=21,
    )
    operations = canonical_staff_operations_config(None)
    operation_parameters = {
        name: _parameter(
            value,
            _OPERATIONS_UNITS[name],
            bounds=([1, 1440] if name.startswith("task_duration") else [0.0, 1.0]),
        )
        for name, value in operations.items()
        if name in _OPERATIONS_UNITS
    }
    operation_parameters["task_duration_minutes.*"] = _parameter(
        operations["task_duration_minutes"],
        "minute_by_registered_task",
        bounds=[1, 1440],
        scope="per_task_kind",
        replaceable=True,
        applicability=(
            "Optional experimental duration for one registered staff task kind; "
            "other task kinds retain their shared durations"
        ),
    )
    operation_parameters["effects_by_zone.*"] = _parameter(
        {},
        "typed_staff_operation_effects_by_zone",
        scope="per_zone_id",
        replaceable=True,
        field_units={
            "sanitation_use_per_person_minute": "normalized_sanitation_index_per_person_minute",
            "cleaning_sanitation_recovery": "normalized_sanitation_index_per_cleaning_task",
            "outreach_uncertainty_relief": "normalized_uncertainty_per_outreach_task",
            "patrol_reassurance": "normalized_reassurance_per_patrol_task",
        },
        field_bounds={
            "sanitation_use_per_person_minute": [0.0, 0.01],
            "cleaning_sanitation_recovery": [0.0, 1.0],
            "outreach_uncertainty_relief": [0.0, 1.0],
            "patrol_reassurance": [0.0, 1.0],
        },
        applicability=(
            "Optional experimental zone-specific sanitation demand and staff task effects; "
            "unspecified zones use the shared coefficients"
        ),
    )
    environment = ScenarioZoneEnvironmentConfig().model_dump(mode="json")
    environment_parameters = {
        name: _parameter(
            value,
            _ENVIRONMENT_UNITS[name],
            bounds=_ENVIRONMENT_BOUNDS.get(name),
        )
        for name, value in environment.items()
        if name in _ENVIRONMENT_UNITS
    }
    environment_parameters["baseline_by_zone.*"] = _parameter(
        {},
        "typed_zone_environment_baseline",
        scope="per_zone_id",
        field_units={
            "ventilation_effectiveness": "normalized_0_1",
            "lighting_level": "normalized_0_1",
            "privacy_level": "normalized_0_1",
            "temperature_c": "degree_celsius",
        },
        field_bounds={
            "ventilation_effectiveness": [0.0, 1.0],
            "lighting_level": [0.0, 1.0],
            "privacy_level": [0.0, 1.0],
            "temperature_c": [-40.0, 60.0],
        },
    )
    return {
        "body_state_v1": _model(
            "life-state-feedback-v1",
            "ASM-LIFE-STATE-FEEDBACK-V1",
            "bounded minute-step need, sleep-debt, hygiene, uncertainty, affect, and trust updates",
            body,
            implementation_version="3",
        ),
        "affect_propagation": {
            model_id: {
                "implementation_version": metadata["implementation_version"],
                "formula": metadata["formula"],
                "reference": metadata["reference"],
                "source_id": metadata["source_id"],
                "parameters": {
                    key: {**value, "evidence": "assumption; no external calibration supplied"}
                    for key, value in metadata["coefficient"].items()
                },
                "evidence": metadata["evidence"],
                "external_validation": False,
            }
            for model_id, metadata in affect_policy_catalog().items()
        },
        "movement_v1": _model(
            "spatial-route-and-room-waypoint-v2",
            "ASM-SPATIAL-MOVEMENT-V2",
            "declared metre-scale route length divided by mobility speed and bounded density "
            "slowdown",
            movement,
        ),
        "staff_dynamics_v1": _model(
            "continuous-duty-fatigue-v1",
            "ASM-STAFF-DYNAMICS-V1",
            "continuous-duty fatigue without shift windows or off-shift recovery",
            {
                name: value
                for name, value in staff_parameters.items()
                if name not in {"shift_windows_by_staff.*", "off_shift_fatigue_recovery_per_minute"}
            },
        ),
        "staff_dynamics_shift_v2": _model(
            "continuous-duty-fatigue-v2",
            "ASM-STAFF-DYNAMICS-V2",
            "fatigue rises with continuous duty and recovers during off-shift or required rest",
            staff_v2_parameters,
            implementation_version="2",
        ),
        "staff_operations_v1": _model(
            "autonomous-routed-staff-v1",
            "ASM-STAFF-OPERATIONS-V1",
            "bounded workload rotation and role-compatible task urgency over reachable routes",
            operation_parameters,
            implementation_version="2",
        ),
        "zone_environment_v1": _model(
            "zone-environment-v2",
            "ASM-H19-ZONE-ENVIRONMENT-V2",
            "active typed condition modifiers feed rest, hygiene, privacy, and temperature stress",
            environment_parameters,
            implementation_version="2",
        ),
        "zone_environment_legacy_v1": _model(
            "zone-environment-v1",
            "ASM-H19-ZONE-ENVIRONMENT-V1",
            "legacy noise and action-demand modifiers without condition feedback",
            {
                name: value
                for name, value in environment_parameters.items()
                if name
                not in {
                    "lighting_sleep_efficiency_loss",
                    "ventilation_hygiene_growth_per_minute",
                    "privacy_discontent_per_minute",
                    "temperature_stress_per_degree_minute",
                    "comfortable_min_temperature_c",
                    "comfortable_max_temperature_c",
                    "baseline_by_zone.*",
                }
            },
        ),
    }


def _model(
    model_id: str,
    source_id: str,
    formula: str,
    parameters: dict[str, dict[str, Any]],
    *,
    implementation_version: str = "1",
) -> dict[str, Any]:
    return {
        "implementation_id": model_id,
        "implementation_version": implementation_version,
        "source_id": source_id,
        "formula": formula,
        "parameters": parameters,
        "evidence": "assumption; replace with an evidence-backed Policy Registry model",
        "external_validation": False,
    }


def _parameter(value: Any, unit: str, **extra: Any) -> dict[str, Any]:
    return {
        "default": value,
        "unit": unit,
        "evidence": "assumption; no external calibration supplied",
        **extra,
    }
