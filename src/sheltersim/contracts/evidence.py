"""Evidence metadata for installed Policies and versioned simulator operators.

Simulator operators remain distinct from interchangeable Policy factory domains.
Registering their parameter evidence must not add an unsupported registry selector.
"""

from __future__ import annotations

from typing import Any

from sheltersim.contracts.policies import model_catalog


def operator_evidence_catalog() -> dict[str, dict[str, Any]]:
    from sheltersim.domain.facilities import TOILET_SERVICE_POLICY_VERSION
    from sheltersim.domain.simulator_state import simulator_state_catalog
    from sheltersim.domain.staff import STAFF_SUPPORT_POLICY_VERSION, STAFF_SUPPORT_SOURCE_ID
    from sheltersim.domain.staff_dynamics import (
        SHIFT_STAFF_DYNAMICS_POLICY_VERSION,
        SHIFT_STAFF_DYNAMICS_SOURCE_ID,
        STAFF_DYNAMICS_POLICY_VERSION,
        STAFF_DYNAMICS_SOURCE_ID,
        canonical_staff_dynamics_config,
    )
    from sheltersim.domain.staff_operations import (
        STAFF_OPERATIONS_POLICY_VERSION,
        canonical_staff_operations_config,
    )
    from sheltersim.experiments.scenario import (
        RESOURCE_SERVICE_POLICY_ID,
        ResourceServiceConfig,
        ToiletServiceConfig,
    )
    from sheltersim.region.conditions import RESOURCE_SERVICE_ASSUMPTION_ID

    catalog = model_catalog()
    record = next(
        item
        for item in simulator_state_catalog().values()
        if item["implementation_id"] == STAFF_OPERATIONS_POLICY_VERSION
    )
    parameters = {}
    for name, value in canonical_staff_operations_config(None).items():
        if isinstance(value, dict):
            for kind, duration in value.items():
                parameters[f"staff_operations.{name}.{kind}"] = {
                    "default": duration,
                    "unit": "minute",
                    "lower": 1,
                    "upper": 1440,
                    "source_id": record["source_id"],
                    "evidence_grade": "C",
                    "applicability": "Synthetic task duration; no empirical calibration",
                }
        elif type(value) in (int, float) and name != "schema_version":
            parameters[f"staff_operations.{name}"] = {
                "default": value,
                "unit": "normalized_per_person_minute"
                if name == "sanitation_use_per_person_minute"
                else "normalized_per_task",
                "lower": 0,
                "upper": 1,
                "source_id": record["source_id"],
                "evidence_grade": "C",
                "applicability": "Synthetic operations coefficient; no empirical calibration",
            }
    catalog[STAFF_OPERATIONS_POLICY_VERSION] = {
        **record,
        "domain": "simulator",
        "parameters": parameters,
        "assumption_source_id": record["source_id"],
        "formula": (
            "Typed routed tasks consume duration/travel/availability; bounded sanitation and "
            "information feedback at completion"
        ),
        "reference": (
            "Installed staff_operations operator; experimental engineering assumptions, no "
            "empirical calibration"
        ),
    }
    from sheltersim.domain.movement import (
        MOVEMENT_POLICY_VERSION,
        MOVEMENT_STATE_KEY,
        canonical_movement_config,
    )

    movement = canonical_movement_config(None)
    movement_record = simulator_state_catalog()[MOVEMENT_STATE_KEY]
    movement_parameters = {}
    for mobility, speed in movement["speed_m_per_minute_by_mobility"].items():
        movement_parameters[f"movement_model.speed_m_per_minute_by_mobility.{mobility}"] = {
            "default": speed,
            "unit": "metre_per_minute",
            "lower": 0,
            "lower_exclusive": True,
            "source_id": movement_record["source_id"],
            "evidence_grade": "C",
            "applicability": "Synthetic mobility speed; no empirical calibration",
        }
    for name, unit, lower in (
        ("density_slowdown_per_person_m2", "square_metre_per_person", 0),
        ("maximum_density_slowdown", "multiplier", 1),
    ):
        movement_parameters[f"movement_model.{name}"] = {
            "default": movement[name],
            "unit": unit,
            "lower": lower,
            "source_id": movement_record["source_id"],
            "evidence_grade": "C",
            "applicability": "Synthetic crowd slowdown; no empirical calibration",
        }
    catalog[MOVEMENT_POLICY_VERSION] = {
        **movement_record,
        "domain": "simulator",
        "parameters": movement_parameters,
        "assumption_source_id": movement_record["source_id"],
        "formula": (
            "Mobility speed and measured density determine route time; room geometry determines "
            "waypoints"
        ),
        "reference": (
            "Installed spatial movement operator; engineering assumptions, not externally validated"
        ),
    }
    toilet_service = ToiletServiceConfig().model_dump(mode="json")
    catalog[TOILET_SERVICE_POLICY_VERSION] = {
        "domain": "simulator",
        "reference": (
            "Installed deterministic FIFO toilet service; duration bounds are synthetic and "
            "uncalibrated"
        ),
        "formula": (
            "Keyed random discrete uniform integer duration between inclusive minimum and "
            "maximum minutes"
        ),
        "assumption_source_id": toilet_service["source_id"],
        "implementation_version": "1",
        "state_schema_version": 1,
        "external_validation": False,
        "parameters": {
            f"toilet_service.{field}": {
                "default": toilet_service[field],
                "unit": "minute",
                "lower": 1,
                "upper": 1440,
                "value_type": "integer",
                "source_id": toilet_service["source_id"],
                "evidence_grade": "C",
                "applicability": (
                    "Synthetic toilet-service duration bound used only when a resident "
                    "starts service; not measured at the selected facility"
                ),
            }
            for field in ("minimum_minutes", "maximum_minutes")
        },
    }
    staff_v1_config = canonical_staff_dynamics_config(None)
    catalog[STAFF_DYNAMICS_POLICY_VERSION] = {
        "domain": "simulator",
        "reference": "Installed continuous-duty staff fatigue and recovery operator",
        "formula": (
            "Continuous work accrues fatigue; required break duration is set by rest_minutes"
        ),
        "assumption_source_id": STAFF_DYNAMICS_SOURCE_ID,
        "implementation_version": "1",
        "state_schema_version": 1,
        "external_validation": False,
        "parameters": {
            "staff_dynamics.rest_minutes": {
                "default": staff_v1_config["rest_minutes"],
                "unit": "minute",
                "lower": 1,
                "upper": 480,
                "value_type": "integer",
                "source_id": STAFF_DYNAMICS_SOURCE_ID,
                "evidence_grade": "C",
                "applicability": (
                    "Required recovery interval after the configured continuous-work limit"
                ),
            },
        },
    }
    shift_config = canonical_staff_dynamics_config(
        {
            "policy_version": SHIFT_STAFF_DYNAMICS_POLICY_VERSION,
            "source_id": SHIFT_STAFF_DYNAMICS_SOURCE_ID,
        }
    )
    shift_parameters = {
        "staff_dynamics.rest_minutes": {
            "default": shift_config["rest_minutes"],
            "unit": "minute",
            "lower": 1,
            "upper": 480,
            "value_type": "integer",
            "source_id": SHIFT_STAFF_DYNAMICS_SOURCE_ID,
            "evidence_grade": "C",
            "applicability": (
                "Required recovery interval after the configured continuous-work limit"
            ),
        },
        "staff_dynamics.off_shift_fatigue_recovery_per_minute": {
            "default": shift_config["off_shift_fatigue_recovery_per_minute"],
            "unit": "normalized_fatigue_per_off_shift_minute",
            "lower": 0.0,
            "upper": 1.0,
            "source_id": SHIFT_STAFF_DYNAMICS_SOURCE_ID,
            "evidence_grade": "C",
            "applicability": (
                "Synthetic off-shift fatigue recovery; active only under staff-dynamics v2"
            ),
        },
        "staff_dynamics.shift_windows_by_staff.*": {
            "default": {},
            "unit": "minute_of_week_intervals",
            "lower": 0,
            "upper": 10080,
            "scope": "per_staff_shift_windows",
            "max_intervals_per_staff": 21,
            "source_id": SHIFT_STAFF_DYNAMICS_SOURCE_ID,
            "evidence_grade": "C",
            "applicability": (
                "Weekly [start,end) on-duty intervals for explicitly named staff IDs; "
                "intervals repeat every week and do not represent measured availability."
            ),
        },
    }
    catalog[SHIFT_STAFF_DYNAMICS_POLICY_VERSION] = {
        "domain": "simulator",
        "reference": "Installed weekly shift and rest-state operator; synthetic staff assumptions",
        "formula": (
            "Weekly staff availability, fatigue threshold recovery breaks and bounded off-shift "
            "recovery"
        ),
        "assumption_source_id": SHIFT_STAFF_DYNAMICS_SOURCE_ID,
        "implementation_version": "2",
        "state_schema_version": 1,
        "external_validation": False,
        "parameters": shift_parameters,
    }
    staff_support_defaults = {
        "support_task_minutes": 10,
        "unresolved_after_minutes": 90,
        "support_cooldown_minutes": 30,
        "support_fairness_minutes": 30,
    }
    staff_support_bounds = {
        "support_task_minutes": (1, 1440),
        "unresolved_after_minutes": (1, 129_600),
        "support_cooldown_minutes": (0, 1440),
        "support_fairness_minutes": (1, 129_600),
    }
    catalog[STAFF_SUPPORT_POLICY_VERSION] = {
        "domain": "simulator",
        "reference": (
            "Installed FIFO support assignment with bounded timeout, task duration, cooldown and "
            "fairness aging"
        ),
        "formula": (
            "FIFO skill-matched assignment with fairness aging and reason-coded request timeout"
        ),
        "assumption_source_id": STAFF_SUPPORT_SOURCE_ID,
        "implementation_version": "2",
        "state_schema_version": 1,
        "external_validation": False,
        "parameters": {
            f"staff.{name}": {
                "default": value,
                "unit": "minute",
                "lower": staff_support_bounds[name][0],
                "upper": staff_support_bounds[name][1],
                "value_type": "integer",
                "source_id": STAFF_SUPPORT_SOURCE_ID,
                "evidence_grade": "C",
                "applicability": (
                    "Synthetic staff-support lifecycle timing; not an operational "
                    "service-level standard"
                ),
            }
            for name, value in staff_support_defaults.items()
        },
    }
    resource_service_id = RESOURCE_SERVICE_POLICY_ID
    resource_service = ResourceServiceConfig(
        schema_version=1,
        policy_version=RESOURCE_SERVICE_POLICY_ID,
        source_id=RESOURCE_SERVICE_ASSUMPTION_ID,
        source_type="assumption",
        service_minutes=5,
        water_ml_per_service=250,
        food_meal_units_per_service=1,
        hydration_growth_per_minute=0.001,
    ).model_dump(mode="json")
    resource_bounds = {
        "service_minutes": (1, 1440),
        "water_ml_per_service": (1, 100_000),
        "food_meal_units_per_service": (1, 100),
        "hydration_growth_per_minute": (0.0, 1.0),
        "hydration_action_threshold": (0.0, 1.0),
        "hunger_action_threshold": (0.0, 1.0),
        "hydration_relief": (0.0, 1.0),
        "hunger_relief": (0.0, 1.0),
        "water_utility_base": (0.0, 10.0),
        "water_utility_need_weight": (0.0, 10.0),
        "meal_utility_base": (0.0, 10.0),
        "meal_utility_need_weight": (0.0, 10.0),
    }
    resource_parameters = {}
    for name, value in resource_service.items():
        if name not in resource_bounds or type(value) not in (int, float):
            continue
        lower, upper = resource_bounds[name]
        resource_parameters[f"resource_service.{name}"] = {
            "default": value,
            "unit": (
                "minute"
                if name == "service_minutes"
                else "mL_per_service"
                if name == "water_ml_per_service"
                else "meal_units_per_service"
                if name == "food_meal_units_per_service"
                else "normalized_per_minute"
                if name == "hydration_growth_per_minute"
                else "utility_weight"
                if "utility" in name
                else "normalized_0_1"
            ),
            "lower": lower,
            "upper": upper,
            "lower_exclusive": name in {"hydration_relief", "hunger_relief"},
            "value_type": "integer"
            if name in {"service_minutes", "water_ml_per_service", "food_meal_units_per_service"}
            else "number",
            "source_id": RESOURCE_SERVICE_ASSUMPTION_ID,
            "evidence_grade": "C",
            "applicability": (
                "Synthetic staffed water/meal service coefficient; applied only when the "
                "opt-in resource service and initial stock are configured"
            ),
        }
    catalog[resource_service_id] = {
        "domain": "simulator",
        "reference": (
            "Installed opt-in staffed resource service; no local consumption or staffing "
            "calibration supplied"
        ),
        "formula": (
            "completion consumes reserved stock and applies bounded service relief; behavior "
            "thresholds and utility weights drive requests"
        ),
        "assumption_source_id": RESOURCE_SERVICE_ASSUMPTION_ID,
        "implementation_version": "1",
        "state_schema_version": 1,
        "external_validation": False,
        "parameters": resource_parameters,
    }
    return catalog
