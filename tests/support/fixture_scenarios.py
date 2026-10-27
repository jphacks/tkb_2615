"""Scenarios built from the fixture files for one model or mechanism."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from support.paths import BASELINE_100_7D, SCENARIO_100_AGENTS, SMALL_REPLAY


def _scenario_with_stock(
    *,
    water_ml: int = 0,
    food_meal_units: int = 0,
    source_ids: list[str] | None = None,
    scheduled_events: list[dict[str, object]] | None = None,
    duration_minutes: int = 5,
) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = scheduled_events or []
    for scheduled in document["scheduled_events"]:
        if scheduled.get("event_type", "").startswith("resource_"):
            scheduled["payload"] = {
                "warehouse_id": "warehouse-main",
                **scheduled.get("payload", {}),
            }
    document["initial_stock"] = {
        "schema_version": 1,
        "warehouse_id": "warehouse-main",
        "zone_id": "sleeping",
        "water_ml": water_ml,
        "food_meal_units": food_meal_units,
        "source_ids": (
            source_ids
            if source_ids is not None
            else (["ASM-H01-TEST-INITIAL-STOCK-V1"] if water_ml or food_meal_units else [])
        ),
    }
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def scenario_with_stock() -> Callable[..., Any]:
    """Build h00_small_replay with an initial stock in warehouse-main and resource events.

    Resource events get warehouse-main in their payload, and a stock other than zero
    gets a test source ID unless source_ids is given.
    """
    return _scenario_with_stock


def _resource_service_scenario(
    *,
    duration: int = 12,
    water_ml: int = 1000,
    food_units: int = 4,
    water_capacity: int = 1,
    meal_capacity: int = 1,
    water_working: int | None = None,
    water_staff_count: int = 1,
    meal_staff: bool = True,
    service_minutes: int = 2,
) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = duration
    document["scheduled_events"] = []
    document["facilities"].extend(
        [
            {
                "id": "water_station_1",
                "kind": "water_station",
                "zone_id": "toilet",
                "capacity": water_capacity,
                "working_units": water_capacity if water_working is None else water_working,
            },
            {
                "id": "meal_distribution_1",
                "kind": "meal_distribution",
                "zone_id": "toilet",
                "capacity": meal_capacity,
                "working_units": meal_capacity,
            },
        ]
    )
    for index in range(water_staff_count):
        document["staff"].append(
            {"id": f"S-WATER-{index + 1}", "zone_id": "toilet", "skills": ["water_service"]}
        )
    if meal_staff:
        document["staff"].append({"id": "S-MEAL", "zone_id": "toilet", "skills": ["meal_service"]})
    document["initial_stock"] = {
        "schema_version": 1,
        "warehouse_id": "warehouse-h02",
        "zone_id": "toilet",
        "water_ml": water_ml,
        "food_meal_units": food_units,
        "source_ids": ["ASM-H02-TEST-INITIAL-STOCK-V1"] if water_ml or food_units else [],
    }
    document["model_configs"]["resource_service"] = {
        "schema_version": 1,
        "policy_version": "resource-service-v1",
        "source_id": "ASM-H02-RESOURCE-SERVICE-V1",
        "source_type": "assumption",
        "service_minutes": service_minutes,
        "water_ml_per_service": 250,
        "food_meal_units_per_service": 1,
        "hydration_growth_per_minute": 0.02,
        "hydration_action_threshold": 0.25,
        "hunger_action_threshold": 0.70,
        "hydration_relief": 0.35,
        "hunger_relief": 0.50,
    }
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def resource_service_scenario() -> Callable[..., Any]:
    """Build h00_small_replay with a staffed water station and meal point in the toilet zone.

    The Scenario opts into resource_service and starts with water and meals in
    warehouse-h02; the arguments change the stock, capacities, staff and duration.
    """
    return _resource_service_scenario


def _single_agent_awareness_scenario(initial_known_facility_ids: list[str]) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["id"] = "facility-awareness-toilet-integration"
    document["duration_minutes"] = 180
    document["scheduled_events"] = []
    document["profiles"] = document["profiles"][:1]
    profile = document["profiles"][0]
    profile["initial_zone_id"] = "sleeping"
    profile["initial_needs"] = {
        "toilet_urgency": 0.65,
        "hunger": 0.45,
        "fatigue": 0.25,
    }
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.01,
        "chronic_retention": 0.9,
        "chronic_acute_weight": 0.08,
        "chronic_high_distress_weight": 0.02,
        "high_distress_threshold": 0.7,
    }
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "mean-reverting-life-state-v1",
    }
    document["model_configs"]["facility_awareness"] = {
        "schema_version": 1,
        "initial_known_facility_ids": initial_known_facility_ids,
        "discover_co_located_facilities_on_arrival": True,
        "source_type": "assumption",
        "source_id": "ASM-FACILITY-ENTRY-ORIENTATION-V1",
    }
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def single_agent_awareness_scenario() -> Callable[[list[str]], Any]:
    """Build a 180-minute scenario_100_agents with only A001, in the sleeping area.

    A001 needs the toilet, the Scenario opts into facility_awareness, and A001
    starts knowing only the facilities given; it discovers the others it reaches.
    """
    return _single_agent_awareness_scenario


def _research_psychology_scenario(*, duration: int = 20, social: bool = False) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["id"] = "research-model-test"
    document["duration_minutes"] = duration
    document["scheduled_events"] = []
    document["profiles"] = document["profiles"][:2]
    for profile in document["profiles"]:
        profile["initial_needs"] = {
            "toilet_urgency": 0.0,
            "hunger": 0.0,
            "fatigue": 0.0,
        }
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.02,
        "chronic_retention": 0.9,
        "chronic_acute_weight": 0.08,
        "chronic_high_distress_weight": 0.02,
        "high_distress_threshold": 0.7,
    }
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "mean-reverting-distress-v1",
    }
    if social:
        document["model_configs"]["social_propagation"] = {
            "enabled": True,
            "max_contacts_per_agent": 1,
            "minimum_contact_minutes": 1,
            "stress_spread_per_minute": 0.2,
            "natural_recovery_per_minute": 0.0,
            "cooldown_minutes": 5,
            "max_pair_stress_increase": 0.25,
            "max_agent_stress_increase_per_minute": 0.05,
            "source_id": "ASM-BOSSE-AFFECT-CONTAGION-V1",
        }
        document["model_configs"]["policy_registry"]["social"] = (
            "expressiveness-channel-openness-v1"
        )
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def research_psychology_scenario() -> Callable[..., Any]:
    """Build a two-person scenario_100_agents with mean-reverting distress and no needs.

    With social=True it also enables the research social operator (Bosse et al.
    affect contagion, expressiveness-channel-openness-v1).
    """
    return _research_psychology_scenario


def _causal_stress_scenario(
    *,
    agent_count: int = 2,
    duration: int = 20,
    stress_pressure: float = 0.1,
    queued_stress: float = 0.01,
    support_wait_stress: float = 0.02,
    social_enabled: bool = False,
    with_service: bool = True,
) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    profiles = [
        {
            "schema_version": 2,
            "id": f"A{index:03}",
            "group_id": "G01",
            "initial_zone_id": "main",
            "support_need": 0.8,
            "stress_sensitivity": 1.0,
            "initial_needs": {"toilet_urgency": 0.8, "hunger": 0.9, "fatigue": 0.2},
            "rng_key": f"profile:A{index:03}",
        }
        for index in range(1, agent_count + 1)
    ]
    config = {
        "psychology": {
            "stress_pressure": stress_pressure,
            "queued_stress": queued_stress,
            "support_wait_stress": support_wait_stress,
        },
        "behavior": {"decision_interval_minutes": 5},
        "staff": {"support_task_minutes": 5, "unresolved_after_minutes": 10},
        "social_propagation": {
            "enabled": social_enabled,
            "max_contacts_per_agent": 1,
            "minimum_contact_minutes": 1,
            "stress_spread_per_minute": 0.01,
            "natural_recovery_per_minute": 0.0,
            "cooldown_minutes": 1,
            "max_pair_stress_increase": 0.05,
            "max_agent_stress_increase_per_minute": 0.05,
        },
    }
    document = {
        "schema_version": 3,
        "id": "causal-stress-test",
        "duration_minutes": duration,
        "seed": 23,
        "zones": [{"id": "main", "name": "Main", "capacity": 10}],
        "profiles": profiles,
        "model_configs": config,
    }
    if with_service:
        document["facilities"] = [
            {"id": "toilet-1", "kind": "toilet", "zone_id": "main", "capacity": 1}
        ]
        document["staff"] = [{"id": "staff-1", "zone_id": "main", "skills": ["support"]}]
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def causal_stress_scenario() -> Callable[..., Any]:
    """Build a one-zone Scenario where needy residents queue for one toilet and one worker.

    The psychology coefficients for need pressure, queueing and support waits can be
    changed; with_service=False drops the toilet and the worker.
    """
    return _causal_stress_scenario


def _information_spread_config(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "enabled": True,
        "max_contacts_per_agent": 16,
        "minimum_contact_minutes": 1,
        "contact_duration_scale_minutes": 1,
        "base_transmission_probability": 1.0,
        "correction_transmission_multiplier": 1.0,
        "trust_sensitivity": 0.0,
        "family_contact_multiplier": 1.0,
        "adjacent_zone_contact_multiplier": 1.0,
        "query_pressure_per_claim": 0.5,
        "query_pressure_decay_per_minute": 0.0,
        "query_pressure_relief_per_correction": 0.5,
        "query_threshold": 0.5,
        "query_utility_weight": 4.0,
    }
    values.update(overrides)
    return values


@pytest.fixture
def information_spread_config() -> Callable[..., dict[str, object]]:
    """An enabled information_spread config where every contact passes a claim on.

    Keyword arguments replace single coefficients.
    """
    return _information_spread_config


def _information_spread_scenario(
    *,
    duration: int = 6,
    profile_count: int = 2,
    truth_status: str = "unknown",
    correction: bool = True,
    config: dict[str, object] | None = None,
) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["id"] = "information-spread-test"
    document["duration_minutes"] = duration
    document["scheduled_events"] = [
        {
            "id": "rumor-event",
            "kind": "exogenous",
            "due_minute": 0,
            "priority": 0,
            "sequence": 10,
            "event_type": "information_rumor",
            "payload": {
                "message_id": "claim-water-1",
                "truth_status": truth_status,
                "origin_agent_id": "A001",
            },
            "source_id": "scenario-rumor-source",
        }
    ]
    if correction:
        document["scheduled_events"].append(
            {
                "id": "correction-event",
                "kind": "exogenous",
                "due_minute": 3,
                "priority": 0,
                "sequence": 11,
                "event_type": "information_correction",
                "payload": {
                    "message_id": "claim-water-1",
                    "zone_id": "sleeping",
                    "belief": "rejected" if truth_status == "false" else "verified",
                },
                "source_id": "staff-notice-source",
                "causation_id": "rumor-event",
            }
        )
    document["profiles"] = document["profiles"][:profile_count]
    for profile in document["profiles"]:
        profile["support_need"] = 0.0
        profile["initial_needs"] = {
            "toilet_urgency": 0.0,
            "hunger": 0.0,
            "fatigue": 0.0,
        }
    document["model_configs"]["information_spread"] = config or _information_spread_config()
    document["model_configs"].setdefault("behavior", {})["temperature"] = 0.01
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def information_spread_scenario() -> Callable[..., Any]:
    """Build scenario_100_agents with a rumor from A001 at minute 0 and, by default, a
    staff correction in the sleeping area at minute 3.

    Residents have no needs, so information is the only thing that drives inquiries.
    """
    return _information_spread_scenario


def _small_replay_scenario(duration_minutes: int = 30) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = []
    return ScenarioSpec.from_json(json.dumps(document))


@pytest.fixture
def small_replay_scenario() -> Callable[..., Any]:
    """Build h00_small_replay without its scheduled events, 30 minutes long by default."""
    return _small_replay_scenario


def _small_benchmark_scenario(duration_minutes: int = 200) -> Any:
    from sheltersim.experiments.benchmark import build_benchmark_scenario
    from sheltersim.experiments.scenario import ScenarioSpec

    # By minute 200 stressed synthetic Agents trigger support-priority and social tasks.
    return build_benchmark_scenario(
        ScenarioSpec.load(BASELINE_100_7D),
        agent_count=30,
        seed=7,
        duration_minutes=duration_minutes,
    )


@pytest.fixture
def small_benchmark_scenario() -> Callable[..., Any]:
    """Build a 30-person, seed 7 benchmark Scenario from baseline_100_7d, 200 minutes by
    default (long enough for stressed people to trigger support-priority and social tasks).
    """
    return _small_benchmark_scenario


def _route_edit_command(
    *,
    edge_id: str = "corridor_toilet",
    travel_minutes: int = 8,
    max_agents_in_transit: int | None = 1,
) -> dict[str, object]:
    return {
        "kind": "set_zone_edge",
        "edge_id": edge_id,
        "travel_minutes": travel_minutes,
        "closed": False,
        "max_agents_in_transit": max_agents_in_transit,
    }


@pytest.fixture
def route_edit_command() -> Callable[..., dict[str, object]]:
    """A layout Branch command that sets corridor_toilet to 8 minutes for one person at a time."""
    return _route_edit_command
