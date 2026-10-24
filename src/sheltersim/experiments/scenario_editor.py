"""Bounded, typed scenario editing for the local demo."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import behavior_policy_catalog

_FIXTURE_DIRECTORY = Path(__file__).parents[3] / "fixtures" / "scenarios"
_PRESET_FILES = {
    "baseline_100_7d": "baseline_100_7d.json",
    "baseline_multi_operational": "baseline_multi_operational.json",
    "delivery_delay": "delivery_delay.json",
    "delivery_intervention": "delivery_intervention.json",
    "failure_day3": "failure_day3.json",
    "multi_operational": "multi_operational.json",
    "water_intervention": "water_intervention.json",
    "water_outage": "water_outage.json",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScenarioOverrides(_StrictModel):
    """Only the bounded operator controls exposed by the demo editor."""

    staff_count: StrictInt | None = Field(default=None, ge=0, le=20)
    toilet_units: StrictInt | None = Field(default=None, ge=0, le=100)
    water_ml: StrictInt | None = Field(default=None, ge=0, le=100_000_000)
    food_meal_units: StrictInt | None = Field(default=None, ge=0, le=1_000_000)
    event_frequency: Literal["preset", "disabled"] | None = None
    social_propagation_enabled: StrictBool | None = None
    behavior_policy: Literal[
        "utility-softmax-v1", "sleep-aware-utility-softmax-v2", "priority-rule-v1"
    ] | None = None


class ScenarioEditorRequest(_StrictModel):
    """A preset plus a small allowlist of human-controlled values."""

    schema_version: Literal[1]
    preset_id: Literal[
        "baseline_100_7d",
        "baseline_multi_operational",
        "delivery_delay",
        "delivery_intervention",
        "failure_day3",
        "multi_operational",
        "water_intervention",
        "water_outage",
    ]
    overrides: ScenarioOverrides = Field(default_factory=ScenarioOverrides)


class FrozenScenarioPreview(_StrictModel):
    """Safe preview metadata; profile records and raw payloads are not returned."""

    schema_version: Literal[1] = 1
    preset_id: str
    scenario_id: str
    scenario_hash: str
    scenario_family_hash: str
    seed: int
    duration_minutes: int
    agent_count: int
    staff_count: int
    toilet_units: int
    water_ml: int | None
    food_meal_units: int | None
    scheduled_event_count: int
    event_frequency: str
    social_propagation_enabled: bool | None
    behavior_policy: str
    model_assumption_ids: list[str] = Field(default_factory=list)
    fields: dict[str, dict[str, str | int | bool | None]]
    source_kind: Literal["operator_configured_scenario"] = "operator_configured_scenario"
    execution_mode: Literal["live_compute"] = "live_compute"

    def to_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def scenario_family_hash(scenario: ScenarioSpec) -> str:
    """Match the run artifact family contract (inputs excluding ID, seed, and event schedule)."""
    family = scenario.model_dump(mode="json")
    family.pop("id", None)
    family.pop("seed", None)
    family.pop("scheduled_events", None)
    return _hash_json(family)


def scenario_hash(scenario: ScenarioSpec) -> str:
    """Hash the canonical, validated ScenarioSpec used by a fresh simulation."""
    return hashlib.sha256(scenario.canonical_json().encode("utf-8")).hexdigest()


def list_scenario_presets() -> list[dict[str, object]]:
    """Return a fixed catalog with safe counts and paired-comparison compatibility."""
    catalog: list[dict[str, object]] = []
    for preset_id, filename in _PRESET_FILES.items():
        scenario = ScenarioSpec.load(_FIXTURE_DIRECTORY / filename)
        if len(scenario.profiles) > 100:
            continue
        toilet_units = sum(
            facility.capacity if facility.working_units is None else facility.working_units
            for facility in scenario.facilities
            if "toilet" in facility.kind.casefold()
        )
        catalog.append(
            {
                "preset_id": preset_id,
                "scenario_id": scenario.id,
                "seed": scenario.seed,
                "duration_minutes": scenario.duration_minutes,
                "agent_count": len(scenario.profiles),
                "staff_count": len(scenario.staff),
                "toilet_units": toilet_units,
                "water_ml": scenario.initial_stock.water_ml
                if scenario.initial_stock is not None
                else None,
                "food_meal_units": scenario.initial_stock.food_meal_units
                if scenario.initial_stock is not None
                else None,
                "scheduled_event_count": len(scenario.scheduled_events),
                "behavior_policy": scenario.model_configs.get("behavior", {}).get(
                    "policy", "utility-softmax-v1"
                ),
                "social_propagation_enabled": scenario.model_configs.get(
                    "social_propagation", {}
                ).get("enabled"),
                "scenario_family_hash": scenario_family_hash(scenario),
                "scenario_hash": scenario_hash(scenario),
                "model_assumption_ids": scenario.provenance.get("model_assumption_ids", []),
                "comparison_group": f"{scenario.seed}:{scenario_family_hash(scenario)}",
                "capabilities": {
                    "staff_count": bool(scenario.staff),
                    "toilet_units": any(
                        "toilet" in item.kind.casefold() for item in scenario.facilities
                    ),
                    "inventory": scenario.initial_stock is not None,
                    "event_frequency": bool(scenario.scheduled_events),
                    "social_propagation": "social_propagation" in scenario.model_configs,
                },
                "behavior_policy_catalog": behavior_policy_catalog(),
                "execution_mode": "live_compute",
                "source_kind": "packaged_synthetic_preset",
            }
        )
    return catalog


def build_scenario_preview(
    request: ScenarioEditorRequest,
) -> tuple[ScenarioSpec, FrozenScenarioPreview]:
    """Build a fully revalidated detached ScenarioSpec without changing any live state."""
    filename = _PRESET_FILES[request.preset_id]
    source = ScenarioSpec.load(_FIXTURE_DIRECTORY / filename)
    if len(source.profiles) > 100:
        raise ValueError("scenario presets are limited to 100 Agents")

    values = source.model_dump(mode="json")
    overrides = request.overrides.model_dump(exclude_none=True)
    source_staff = sorted(values["staff"], key=lambda item: item["id"])
    if "staff_count" in overrides:
        target = overrides["staff_count"]
        if target and not source_staff:
            raise ValueError("this preset has no staff template to extend")
        values["staff"] = source_staff[:target]
        while len(values["staff"]) < target:
            template = source_staff[0]
            index = len(values["staff"]) + 1
            values["staff"].append(
                {
                    **template,
                    "id": f"OP-S{index:02d}",
                    "skills": list(template["skills"]),
                }
            )

    if "toilet_units" in overrides:
        toilet_facilities = sorted(
            (item for item in values["facilities"] if "toilet" in item["kind"].casefold()),
            key=lambda item: item["id"],
        )
        if not toilet_facilities:
            raise ValueError("this preset has no toilet facilities to configure")
        count, remainder = divmod(overrides["toilet_units"], len(toilet_facilities))
        for index, facility in enumerate(toilet_facilities):
            units = count + (1 if index < remainder else 0)
            facility["capacity"] = units
            facility["working_units"] = units

    if "water_ml" in overrides or "food_meal_units" in overrides:
        stock = values.get("initial_stock")
        if stock is None:
            raise ValueError("inventory can only be edited in a preset with initial_stock")
        for field in ("water_ml", "food_meal_units"):
            if field in overrides:
                stock[field] = overrides[field]

    if overrides.get("event_frequency") == "disabled":
        values["scheduled_events"] = []

    if "social_propagation_enabled" in overrides:
        config = values["model_configs"].get("social_propagation")
        if config is None:
            raise ValueError("this preset has no social_propagation model to configure")
        config["enabled"] = overrides["social_propagation_enabled"]

    if "behavior_policy" in overrides:
        behavior = values["model_configs"].setdefault("behavior", {})
        behavior["policy"] = overrides["behavior_policy"]
        behavior["source_id"] = (
            "ASM-PRIORITY-RULE-BEHAVIOR-V1"
            if overrides["behavior_policy"] == "priority-rule-v1"
            else (
                "ASM-UTILITY-BEHAVIOR-V1"
                if overrides["behavior_policy"] == "utility-softmax-v1"
                else "ASM-SLEEP-AWARE-BEHAVIOR-V2"
            )
        )

    option_hash = _hash_json({"preset_id": request.preset_id, "overrides": overrides})[:12]
    values["id"] = f"{request.preset_id}-{option_hash}"
    scenario = ScenarioSpec.model_validate(values)
    if "behavior_policy" in overrides:
        scenario = scenario.with_behavior_policy_variant(overrides["behavior_policy"])
    if (
        "behavior_policy" not in overrides
        or overrides["behavior_policy"] == "sleep-aware-utility-softmax-v2"
    ):
        from sheltersim.experiments.model_suite import apply_default_model_suite

        scenario = apply_default_model_suite(scenario, force=True)
    if len(scenario.profiles) > 100:
        raise ValueError("scenario editor cannot create more than 100 Agents")

    toilets = [item for item in scenario.facilities if "toilet" in item.kind.casefold()]
    stock = scenario.initial_stock
    scheduled_count = len(scenario.scheduled_events)
    social_enabled = scenario.model_configs.get("social_propagation", {}).get("enabled")
    assumption_ids = scenario.provenance.get("model_assumption_ids", [])
    behavior_policy = scenario.model_configs.get("behavior", {}).get("policy", "utility-softmax-v1")
    fields: dict[str, dict[str, str | int | bool | None]] = {
        key: {
            "value": overrides.get(key, _preset_value(source, key)),
            "origin": "operator" if key in overrides else "preset",
        }
        for key in (
            "staff_count",
            "toilet_units",
            "water_ml",
            "food_meal_units",
            "event_frequency",
            "social_propagation_enabled",
            "behavior_policy",
        )
    }
    preview = FrozenScenarioPreview(
        preset_id=request.preset_id,
        scenario_id=scenario.id,
        scenario_hash=scenario_hash(scenario),
        scenario_family_hash=scenario_family_hash(scenario),
        seed=scenario.seed,
        duration_minutes=scenario.duration_minutes,
        agent_count=len(scenario.profiles),
        staff_count=len(scenario.staff),
        toilet_units=sum(
            item.capacity if item.working_units is None else item.working_units for item in toilets
        ),
        water_ml=stock.water_ml if stock is not None else None,
        food_meal_units=stock.food_meal_units if stock is not None else None,
        scheduled_event_count=scheduled_count,
        event_frequency="disabled" if not scheduled_count else "preset",
        social_propagation_enabled=social_enabled,
        behavior_policy=behavior_policy,
        model_assumption_ids=[item for item in assumption_ids if isinstance(item, str)],
        fields=fields,
    )
    return scenario, preview


def _preset_value(scenario: ScenarioSpec, key: str) -> str | int | bool | None:
    if key == "staff_count":
        return len(scenario.staff)
    if key == "toilet_units":
        return sum(
            item.capacity if item.working_units is None else item.working_units
            for item in scenario.facilities
            if "toilet" in item.kind.casefold()
        )
    if key in {"water_ml", "food_meal_units"}:
        return getattr(scenario.initial_stock, key) if scenario.initial_stock else None
    if key == "event_frequency":
        return "preset" if scenario.scheduled_events else "disabled"
    if key == "social_propagation_enabled":
        return scenario.model_configs.get("social_propagation", {}).get("enabled")
    if key == "behavior_policy":
        return scenario.model_configs.get("behavior", {}).get("policy", "utility-softmax-v1")
    raise ValueError(f"unknown scenario editor field: {key}")


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
