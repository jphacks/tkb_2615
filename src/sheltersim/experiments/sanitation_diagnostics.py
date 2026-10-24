"""Planning-only sanitation diagnostics based on the Japanese Cabinet Office guide."""

from __future__ import annotations

import math
from typing import Any

from sheltersim.experiments.scenario import ScenarioSpec

PEOPLE_PER_TOILET = {"initial": 50, "long-term": 20}
PLANNING_TOILET_USES_PER_PERSON_DAY = 5


def toilet_planning_diagnostic(scenario: ScenarioSpec, *, phase: str = "initial") -> dict[str, Any]:
    """Compare declared working toilet units with planning benchmarks, not behavior."""
    if phase not in PEOPLE_PER_TOILET:
        raise ValueError("phase must be 'initial' or 'long-term'")
    population = len(scenario.profiles)
    available_units = sum(
        facility.working_units if facility.working_units is not None else facility.capacity
        for facility in scenario.facilities
        if facility.kind == "toilet"
    )
    people_per_toilet = PEOPLE_PER_TOILET[phase]
    target_units = math.ceil(population / people_per_toilet) if population else 0
    planned_daily_uses = population * PLANNING_TOILET_USES_PER_PERSON_DAY
    shortage = max(0, target_units - available_units)
    warnings = []
    if available_units < target_units:
        warnings.append(
            f"利用可能トイレ数が計画目安を下回ります（{available_units}基、目安{target_units}基）。"
        )
    if available_units == 0 and population:
        warnings.append("利用可能なトイレがありません。")
    return {
        "scenario_id": scenario.id,
        "phase": phase,
        "population": population,
        "available_working_toilet_units": available_units,
        "planning_people_per_toilet": people_per_toilet,
        "planning_target_toilet_units": target_units,
        "shortage_units": shortage,
        "planning_uses_per_person_day": PLANNING_TOILET_USES_PER_PERSON_DAY,
        "planning_total_uses_per_day": planned_daily_uses,
        "warnings": warnings,
        "source": "Cabinet Office, Shelter Toilet Provision and Management Guideline (2024-12)",
        "source_id": "SRC-CAO-TOILET-GUIDELINE-2024-12",
        "interpretation": (
            "設備計画の確認値です。個人の排泄頻度、行動選択確率、サービス時間を"
            "決定する係数ではありません。実際の利用可能性と設備状態は運営者が確認してください。"
        ),
    }
