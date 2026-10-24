"""Offline held-out evaluation for truthful adaptive Event Compiler outputs."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.environment import (
    ScenarioZoneEnvironmentConfig,
    zone_environment_view,
)
from sheltersim.domain.operational import staff_is_available, water_service_blocked
from sheltersim.domain.resources import resource_metric_values
from sheltersim.events.effects import (
    EffectProgramEnvironmentHandler,
    EffectProgramFacilityHandler,
    EffectProgramStructureHandler,
    validate_effect_program,
)
from sheltersim.events.handlers import (
    DeliveryDelayHandler,
    EventDispatcher,
    ResourceLedgerHandler,
    StaffRestorationHandler,
    StaffUnavailabilityHandler,
    WaterOutageHandler,
    WaterRestorationHandler,
)
from sheltersim.events.models import EventStatus
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.metrics import MetricSnapshot
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import ProviderResponse
from sheltersim.llm.natural_language import compile_natural_language, scenario_summary_from_world
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.schemas import ParseRequest

ROOT = Path(__file__).parents[1]
SMALL_SCENARIO = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"
EVALUATION = json.loads(
    (ROOT / "fixtures" / "evaluation" / "unseen_prompts_ja.json").read_text(encoding="utf-8")
)
EXPECTED_CATEGORIES = {
    "physical",
    "resource",
    "environment",
    "crowd",
    "behavior",
    "social",
    "unknown",
}


class _RecordedProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        del task, max_output_tokens, timeout_seconds
        self.calls += 1
        return ProviderResponse(
            text=self.response,
            model_id=model_id,
            input_tokens=0,
            output_tokens=0,
            elapsed_ms=0.0,
        )


class _UnexpectedProvider:
    def complete(self, **kwargs):
        del kwargs
        raise AssertionError("REPLAY must use the recorded response tape")


def _world() -> WorldState:
    scenario = json.loads(SMALL_SCENARIO.read_text(encoding="utf-8"))
    scenario["schema_version"] = 3
    scenario["duration_minutes"] = 14_400
    scenario["scheduled_events"] = [
        {
            "id": "h20-pending-shipment-event",
            "kind": "exogenous",
            "due_minute": 9_000,
            "priority": 0,
            "sequence": 0,
            "event_type": "resource_delivery",
            "payload": {
                "warehouse_id": "warehouse-h20",
                "resource_id": "water_ml",
                "quantity": 100,
                "shipment_id": "shipment-h20",
            },
            "source_id": "ASM-H20-EVAL-SHIPMENT-V1",
        }
    ]
    scenario["initial_stock"] = {
        "schema_version": 1,
        "warehouse_id": "warehouse-h20",
        "zone_id": "toilet",
        "water_ml": 1_000,
        "food_meal_units": 10,
        "source_ids": ["ASM-H20-EVAL-STOCK-V1"],
    }
    scenario["model_configs"]["resource_service"] = {
        "schema_version": 1,
        "policy_version": "resource-service-v1",
        "source_id": "ASM-H02-RESOURCE-SERVICE-V1",
        "source_type": "assumption",
        "service_minutes": 2,
        "water_ml_per_service": 250,
        "food_meal_units_per_service": 1,
        "hydration_growth_per_minute": 0.02,
    }
    scenario["model_configs"]["zone_environment"] = ScenarioZoneEnvironmentConfig().model_dump(
        mode="json"
    )
    scenario["facilities"].extend(
        [
            {
                "id": "water-station-h20",
                "kind": "water_station",
                "zone_id": "toilet",
                "capacity": 1,
            },
            {
                "id": "meal-station-h20",
                "kind": "meal_distribution",
                "zone_id": "toilet",
                "capacity": 1,
            },
        ]
    )
    scenario["staff"].extend(
        [
            {"id": "water-staff-h20", "skills": ["water_service"], "zone_id": "toilet"},
            {"id": "meal-staff-h20", "skills": ["meal_service"], "zone_id": "toilet"},
        ]
    )
    return WorldState.from_scenario(ScenarioSpec.model_validate(scenario))


def _recorded_json(case: dict[str, object]) -> str:
    """Expand the compact recorded fixture into the compiler's strict JSON shape."""
    return json.dumps(case["recorded_response"], ensure_ascii=False, separators=(",", ":"))


def _router(response: str, tape: ResponseTape, *, replay: bool = False) -> tuple[LLMRouter, object]:
    provider = _UnexpectedProvider() if replay else _RecordedProvider(response)
    router = LLMRouter(
        LLMSettings(mode=LLMMode.REPLAY if replay else LLMMode.RECORD, budget_usd=1.0),
        provider=provider,
        tape=tape,
    )
    return router, provider


def _dispatcher() -> EventDispatcher:
    return EventDispatcher(
        {
            "effect_program_apply": EffectProgramFacilityHandler(),
            "effect_program_expire": EffectProgramFacilityHandler(expire=True),
            "effect_program_structure": EffectProgramStructureHandler(),
            "effect_program_environment": EffectProgramEnvironmentHandler(),
            "effect_program_environment_expire": EffectProgramEnvironmentHandler(expire=True),
            "resource_delivery": ResourceLedgerHandler("delivery"),
            "delivery_delayed": DeliveryDelayHandler(),
            "staff_unavailable": StaffUnavailabilityHandler(),
            "staff_restored": StaffRestorationHandler(),
            "water_outage": WaterOutageHandler(),
            "water_restored": WaterRestorationHandler(),
        }
    )


def _observe(world: WorldState, expected: dict[str, object]) -> int | float | bool | None:
    kind = expected["kind"]
    target = expected.get("target")
    if kind == "metric":
        return getattr(MetricSnapshot.from_world(world), str(expected["field"]))
    if kind == "resource_metric":
        values = resource_metric_values(world)
        assert values is not None
        return values[str(expected["field"])]
    if kind == "shipment_due_minute":
        return world.model_state["operational_events_v1"]["shipments"][str(target)]["due_minute"]
    if kind == "water_service_blocked":
        return water_service_blocked(world, str(target))
    if kind == "staff_available":
        return staff_is_available(world, str(target))
    if kind == "zone_noise_recovery":
        return zone_environment_view(world, str(target)).rest_efficiency_multiplier
    if kind == "zone_demand_probability":
        view = zone_environment_view(world, str(target))
        demand = next(
            (item for item in view.demand_modifiers if item.action == expected["action"]),
            None,
        )
        return demand.probability_multiplier if demand else None
    raise AssertionError(f"unrecognized recorded runtime observation {kind!r}")


def _assert_kpi_deltas(
    before: WorldState,
    after: WorldState,
    expected: dict[str, int | float],
) -> None:
    before_metrics = asdict(MetricSnapshot.from_world(before))
    after_metrics = asdict(MetricSnapshot.from_world(after))
    before_resources = resource_metric_values(before) or {}
    after_resources = resource_metric_values(after) or {}
    assert before_metrics["source_ids_json"] != after_metrics["source_ids_json"]
    for name, after_value in after_metrics.items():
        if name in {"minute_index", "source_ids_json"}:
            continue
        delta = after_value - before_metrics[name]
        assert delta == expected.get(name, 0), f"unexpected KPI delta for {name}: {delta}"
    for name, after_value in after_resources.items():
        delta = after_value - before_resources[name]
        assert delta == expected.get(name, 0), f"unexpected resource KPI delta for {name}: {delta}"
    for name in expected.keys() - before_metrics.keys() - before_resources.keys():
        raise AssertionError(f"unknown expected KPI field {name!r}")


def test_held_out_set_covers_the_declared_domains_without_prompt_specific_rules() -> None:
    cases = EVALUATION["cases"]
    assert len(cases) >= 20
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["category"] for case in cases} == EXPECTED_CATEGORIES
    assert all(
        sum(item["category"] == category for item in cases) >= 2 for category in EXPECTED_CATEGORIES
    )
    assert all(case["recorded_response"]["status"] == case["expected"]["status"] for case in cases)
    assert all(
        case["recorded_response"]["support_mode"] == case["expected"]["support_mode"]
        for case in cases
    )


@pytest.mark.parametrize("case", EVALUATION["cases"], ids=lambda item: item["id"])
def test_recorded_unseen_outputs_validate_replay_and_preserve_source(
    case: dict[str, object],
) -> None:
    original = _world()
    original_before = original.canonical_json()
    summary = scenario_summary_from_world(original)
    summary_before = summary.model_dump(mode="json")
    request = ParseRequest(
        text=str(case["story"]),
        branch_id="baseline",
        selected_minute=0,
        scenario_summary=summary,
    )
    tape = ResponseTape()
    response = _recorded_json(case)
    router, provider = _router(response, tape)

    result = compile_natural_language(request, router)

    expected = case["expected"]
    assert result.status == expected["status"]
    assert result.support_mode.value == expected["support_mode"]
    assert provider.calls == 1
    assert router.api_request_count == 1  # one fake call; the provider has no network client
    assert result.usage.estimated_cost_usd == 0.0
    assert original.canonical_json() == original_before
    assert summary.model_dump(mode="json") == summary_before

    replay_router, replay_provider = _router("unused", tape, replay=True)
    replayed = compile_natural_language(request, replay_router)
    assert replay_provider.__class__ is _UnexpectedProvider
    assert replay_router.api_request_count == 0
    assert replayed.usage.cache_hit is True
    assert replayed.status == result.status
    assert replayed.support_mode == result.support_mode
    if result.draft_plan is None:
        assert expected["operators"] == []
        assert replayed.draft_plan is None
        assert result.status in {"clarification_required", "not_simulatable"}
        assert original.canonical_json() == original_before
        return

    plan = result.draft_plan
    assert sorted({effect.op for effect in plan.effects}) == sorted(expected["operators"])
    assert not validate_effect_program(original, plan), (
        "accepted output must pass runtime preflight"
    )
    assert plan.model_dump(mode="json") == replayed.draft_plan.model_dump(mode="json")

    branch = BranchRuntime.from_world(original, branch_id="evaluation")
    scheduled = branch.apply_effect_program(plan)
    fork = BranchRuntime.from_checkpoint(branch.checkpoint(), branch_id="evaluation-replay")
    assert fork.world.canonical_json() == branch.world.canonical_json()
    assert original.canonical_json() == original_before

    start_minute = min(effect.start_minute for effect in plan.effects)
    branch.world.minute_index = start_minute
    before_observation = _observe(branch.world, case["runtime_observation"])
    records = _dispatcher().dispatch_due(branch.world, start_minute)
    assert len(records) == len(plan.effects)
    assert all(record.status is EventStatus.APPLIED for record in records)
    assert {record.event.event_id for record in records} <= set(scheduled.event_ids)
    assert (
        _observe(branch.world, case["runtime_observation"]) == case["runtime_observation"]["after"]
    )
    assert before_observation == case["runtime_observation"]["before"]
    _assert_kpi_deltas(original_at_minute(original, start_minute), branch.world, case["kpi_delta"])

    traced_effect_ids = {
        record.event.provenance["effect_program_v1"]["effect_id"] for record in records
    }
    assert traced_effect_ids == {effect.effect_id for effect in plan.effects}
    assert original.canonical_json() == original_before


def original_at_minute(world: WorldState, minute: int) -> WorldState:
    """Create a KPI-only baseline at the same sample time without changing its source."""
    baseline = WorldState.from_dict(world.to_dict())
    baseline.minute_index = minute
    return baseline
