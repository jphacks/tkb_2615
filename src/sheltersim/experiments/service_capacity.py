"""Planning-only check that facility units can serve the modeled daily demand.

The check divides each service's daily work (people x uses per day x minutes per use)
by the work its units can do in the assumed operating window. It does not change
behavior, and the demand rates are approximations of the Scenario's own coefficients,
not observed shelter use.
"""

from __future__ import annotations

import math
from typing import Any

from sheltersim.experiments.sanitation_diagnostics import (
    PEOPLE_PER_TOILET,
    PLANNING_TOILET_USES_PER_PERSON_DAY,
)
from sheltersim.experiments.scenario import (
    PsychologyModelConfig,
    ResourceServiceConfig,
    ScenarioSpec,
    ToiletServiceConfig,
)

SERVICE_CAPACITY_SOURCE_ID = "ASM-SERVICE-CAPACITY-PLANNING-V1"
TOILET_GUIDELINE_SOURCE_ID = "SRC-CAO-TOILET-GUIDELINE-2024-12"
# Demand is assumed to fall in 16 waking hours; a unit busier than 80% of that window
# builds long queues even before peaks.
OPERATING_MINUTES_PER_DAY = 960
TARGET_UTILIZATION = 0.8


def required_service_units(daily_service_minutes: float) -> int:
    """Return the units that keep the daily work within the target load."""
    if daily_service_minutes <= 0:
        return 0
    return math.ceil(daily_service_minutes / (OPERATING_MINUTES_PER_DAY * TARGET_UTILIZATION))


def service_capacity_diagnostic(
    scenario: ScenarioSpec, *, phase: str = "initial"
) -> dict[str, Any]:
    """Compare toilet, water and meal units with the work their daily demand needs."""
    if phase not in PEOPLE_PER_TOILET:
        raise ValueError("phase must be 'initial' or 'long-term'")
    population = len(scenario.profiles)
    configs = scenario.model_configs
    toilet = ToiletServiceConfig.model_validate(configs.get("toilet_service", {}))
    rows = [
        _row(
            scenario,
            service="toilet",
            facility_kind="toilet",
            uses_per_person_day=PLANNING_TOILET_USES_PER_PERSON_DAY,
            demand_basis="Cabinet Office planning figure: 5 uses per person per day",
            service_minutes=(toilet.minimum_minutes + toilet.maximum_minutes) / 2,
            staff_skill=None,
            standard_units=math.ceil(population / PEOPLE_PER_TOILET[phase]) if population else 0,
        )
    ]
    raw_resource = configs.get("resource_service")
    if raw_resource is None:
        rows.extend(
            _not_modeled(service, kind)
            for service, kind in (("water", "water_station"), ("meal", "meal_distribution"))
        )
    else:
        resource = ResourceServiceConfig.model_validate(raw_resource)
        psychology = PsychologyModelConfig.model_validate(configs.get("psychology", {}))
        hunger_rates = [
            psychology.hunger_growth_by_agent.get(profile.id, psychology.hunger_growth)
            for profile in scenario.profiles
        ]
        rows.append(
            _row(
                scenario,
                service="water",
                facility_kind="water_station",
                uses_per_person_day=_uses_per_day(
                    resource.hydration_growth_per_minute,
                    relief=resource.hydration_relief,
                    action_threshold=resource.hydration_action_threshold,
                ),
                demand_basis=(
                    "hydration_growth_per_minute x 1440 / "
                    "min(hydration_relief, hydration_action_threshold)"
                ),
                service_minutes=resource.service_minutes,
                staff_skill=resource.water_staff_skill,
                standard_units=None,
            )
        )
        rows.append(
            _row(
                scenario,
                service="meal",
                facility_kind="meal_distribution",
                uses_per_person_day=_uses_per_day(
                    sum(hunger_rates) / len(hunger_rates) if hunger_rates else 0.0,
                    relief=resource.hunger_relief,
                    action_threshold=resource.hunger_action_threshold,
                ),
                demand_basis=(
                    "mean hunger_growth x 1440 / min(hunger_relief, hunger_action_threshold)"
                ),
                service_minutes=resource.service_minutes,
                staff_skill=resource.meal_staff_skill,
                standard_units=None,
            )
        )
    return {
        "scenario_id": scenario.id,
        "phase": phase,
        "population": population,
        "operating_minutes_per_day": OPERATING_MINUTES_PER_DAY,
        "target_utilization": TARGET_UTILIZATION,
        "services": rows,
        "warnings": [warning for row in rows for warning in row["warnings"]],
        "source_id": SERVICE_CAPACITY_SOURCE_ID,
        "interpretation": (
            "設備計画の確認値です。需要はScenarioの係数からの近似で、時間帯の集中や故障を含みません。"
            "利用率が1以上なら、休みなく稼働しても1日の需要を処理できません。"
        ),
    }


def _uses_per_day(growth_per_minute: float, *, relief: float, action_threshold: float) -> float:
    # Agents act only once the need reaches the threshold and relief stops at zero,
    # so one service removes at least min(relief, threshold): an upper bound on uses.
    removed = min(relief, action_threshold) if action_threshold > 0 else relief
    return growth_per_minute * 1440 / removed


def _working_units(facility: Any) -> int:
    return facility.working_units if facility.working_units is not None else facility.capacity


def _row(
    scenario: ScenarioSpec,
    *,
    service: str,
    facility_kind: str,
    uses_per_person_day: float,
    demand_basis: str,
    service_minutes: float,
    staff_skill: str | None,
    standard_units: int | None,
) -> dict[str, Any]:
    facilities = [item for item in scenario.facilities if item.kind == facility_kind]
    units = sum(_working_units(item) for item in facilities)
    if staff_skill is None:
        serving_units = units
    else:
        # A staffed service starts only with a free, skilled staff member in its zone.
        serving_units = 0
        for zone_id in sorted({item.zone_id for item in facilities}):
            zone_units = sum(_working_units(item) for item in facilities if item.zone_id == zone_id)
            zone_staff = sum(
                1
                for member in scenario.staff
                if member.zone_id == zone_id and staff_skill in member.skills
            )
            serving_units += min(zone_units, zone_staff)
    population = len(scenario.profiles)
    daily_uses = population * uses_per_person_day
    daily_minutes = daily_uses * service_minutes
    available_minutes = serving_units * OPERATING_MINUTES_PER_DAY
    utilization = daily_minutes / available_minutes if available_minutes else None
    required_units = required_service_units(daily_minutes)
    label = {"toilet": "トイレ", "water": "給水", "meal": "配食"}[service]
    warnings: list[str] = []
    if daily_minutes > 0 and units == 0:
        status = "no_units"
        warnings.append(f"{label}の設備がありません。")
    elif daily_minutes > 0 and serving_units == 0:
        status = "no_staff"
        warnings.append(
            f"{label}設備のzoneに担当技能（{staff_skill}）を持つ職員がいないため、"
            f"{label}は一度も始まりません。"
        )
    elif utilization is not None and utilization >= 1:
        status = "structurally_short"
        warnings.append(
            f"{label}は休みなく稼働しても1日の需要を処理できません"
            f"（利用率{utilization:.2f}、稼働{serving_units}、目安{required_units}）。"
        )
    elif utilization is not None and utilization > TARGET_UTILIZATION:
        status = "above_target"
        warnings.append(
            f"{label}の利用率が目標{TARGET_UTILIZATION:g}を超え、長い待ち行列が起きやすい状態です"
            f"（利用率{utilization:.2f}、稼働{serving_units}、目安{required_units}）。"
        )
    else:
        status = "ok"
    if standard_units is not None and units < standard_units:
        warnings.append(
            f"{label}が公的な計画目安を下回ります（{units}基、目安{standard_units}基）。"
        )
    return {
        "service": service,
        "facility_kind": facility_kind,
        "status": status,
        "units": units,
        "serving_units": serving_units,
        "uses_per_person_day": uses_per_person_day,
        "demand_basis": demand_basis,
        "service_minutes": service_minutes,
        "daily_uses": daily_uses,
        "daily_service_minutes": daily_minutes,
        "utilization": utilization,
        "required_units": required_units,
        "standard_units": standard_units,
        "source_ids": (
            [SERVICE_CAPACITY_SOURCE_ID, TOILET_GUIDELINE_SOURCE_ID]
            if service == "toilet"
            else [SERVICE_CAPACITY_SOURCE_ID]
        ),
        "warnings": warnings,
    }


def _not_modeled(service: str, facility_kind: str) -> dict[str, Any]:
    return {
        "service": service,
        "facility_kind": facility_kind,
        "status": "not_modeled",
        "reason": "resource_service is not enabled; no stock, demand or service is simulated",
        "warnings": [],
    }
