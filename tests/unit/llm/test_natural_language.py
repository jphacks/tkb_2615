"""Offline coverage for the Opus Event Compiler and its truthful fallback contract, and
the offline typed DraftPlan parser, which never calls a provider or changes state and
lets an explicit zone ID win over common words.

The held-out evaluation compiles unseen Japanese prompts (fixtures/evaluation) with
recorded replies and checks that each applied program moves the expected KPI.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

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
from sheltersim.llm.config import LLMErrorPolicy, LLMMode, LLMSettings, ModelPrice
from sheltersim.llm.contracts import LLMProviderError, LLMStage, ProviderResponse
from sheltersim.llm.natural_language import (
    InterventionCompilerUnavailable,
    compile_natural_language,
    parse_natural_language,
    scenario_summary_from_world,
)
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.schemas import (
    DraftPlan,
    EffectProgram,
    FacilityTarget,
    ParseRequest,
    ScenarioSummary,
    ZoneTarget,
)

FIXTURE = Path(__file__).parents[3] / "fixtures" / "scenario_100_agents.json"
SMALL_SCENARIO = Path(__file__).parents[3] / "fixtures" / "scenarios" / "h00_small_replay.json"
EVALUATION = json.loads(
    (Path(__file__).parents[3] / "fixtures" / "evaluation" / "unseen_prompts_ja.json").read_text(
        encoding="utf-8"
    )
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


class FakeProvider:
    def __init__(self, *responses: str, error: Exception | None = None) -> None:
        self.responses = list(responses)
        self.error = error
        self.calls = []

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        self.calls.append((model_id, task))
        if self.error is not None:
            raise self.error
        response = self.responses.pop(0)
        return ProviderResponse(
            text=response,
            model_id=model_id,
            input_tokens=120,
            output_tokens=80,
            elapsed_ms=12.0,
        )


def summary(*, with_water_policy: bool = True) -> ScenarioSummary:
    policies = (
        {"facility_capacity", "staff_operations", "operational_events_v1", "resource_service_v1"}
        if with_water_policy
        else set()
    )
    return ScenarioSummary(
        duration_minutes=14_400,
        start_date=date(2026, 10, 12),
        agent_count=12,
        facilities=[
            FacilityTarget(
                id="toilet-1",
                name="North restroom",
                kind="toilet",
                zone_id="main",
                capacity=4,
                working_units=4,
            ),
            FacilityTarget(
                id="water-1",
                name="Main water station",
                kind="water_station",
                zone_id="main",
                capacity=2,
                working_units=2,
            ),
        ],
        zones=[ZoneTarget(id="main", name="Main hall", has_water_station=True)],
        staff_ids=["staff-1"],
        available_staff_ids=["staff-1"],
        available_policies=sorted(policies),
    )


def request(text: str = "Day 3 深夜に水場の供給が止まり、当番チームは2人増える") -> ParseRequest:
    return ParseRequest(
        text=text,
        branch_id="baseline",
        selected_minute=0,
        scenario_summary=summary(),
    )


def composite_response() -> str:
    return json.dumps(
        {
            "status": "compiled",
            "support_mode": "composed",
            "mechanism_summary": "給水停止と支援要員追加の2作用として編成しました。",
            "effects": [
                {
                    "op": "water_outage",
                    "target_kind": "zone",
                    "target_id": "main",
                    "start_minute": 3000,
                    "end_minute": 3120,
                    "parameters": {"outage": True},
                    "assumption_indexes": [],
                },
                {
                    "op": "add_staff",
                    "target_kind": "zone",
                    "target_id": "main",
                    "start_minute": 3000,
                    "parameters": {"count": 2},
                    "assumption_indexes": [],
                },
            ],
            "assumptions": [],
            "limitations": [],
            "questions": [],
            "choices": [],
        },
        ensure_ascii=False,
    )


def not_simulatable_response(reason: str = "必要な作用モデルがありません。") -> str:
    return json.dumps(
        {
            "status": "not_simulatable",
            "support_mode": "not_simulatable",
            "mechanism_summary": "対応する作用を特定できません。",
            "effects": [],
            "assumptions": [],
            "limitations": [reason],
            "questions": [],
            "choices": [],
        },
        ensure_ascii=False,
    )


def router(provider: FakeProvider, *, mode: LLMMode = LLMMode.RECORD, tape=None) -> LLMRouter:
    return LLMRouter(
        LLMSettings(
            mode=mode,
            api_key="test-key" if mode is LLMMode.RECORD else None,
            max_api_calls=2,
            max_input_tokens=16_384,
            max_output_tokens=4096,
            budget_usd=1.0,
            opus_price=ModelPrice(1, 2),
            haiku_price=ModelPrice(0.1, 0.5),
        ),
        provider=provider,
        tape=tape or ResponseTape(),
    )


def test_opus_compiles_unseen_narrative_into_multiple_installed_primitives() -> None:
    provider = FakeProvider(composite_response())
    active_router = router(provider)

    result = compile_natural_language(request(), active_router)

    assert result.status == "compiled"
    assert result.support_mode == "composed"
    assert result.draft_plan is not None
    assert {effect.op for effect in result.draft_plan.effects} == {"water_outage", "add_staff"}
    assert result.draft_plan.fork_at_minute == 0
    assert result.draft_plan.requires_confirmation is True
    assert result.draft_plan.compiler_provenance.compiler_kind == "llm"
    assert result.usage.model_id == active_router.settings.opus_model
    assert result.usage.api_request_count == 1
    assert result.preview_notice.endswith("観測結果ではありません。")

    model_id, task = provider.calls[0]
    payload = json.loads(task.input_text)
    assert model_id == active_router.settings.opus_model
    assert task.stage is LLMStage.INTERVENTION
    assert payload["scenario"]["agent_count"] == 12
    assert payload["scenario"]["available_policies"] == sorted(
        {"facility_capacity", "staff_operations", "operational_events_v1", "resource_service_v1"}
    )
    assert {item["op"] for item in payload["installed_primitives"]} >= {
        "water_outage",
        "add_staff",
    }


def test_opus_replay_uses_tape_without_calling_provider_and_keeps_same_plan() -> None:
    tape = ResponseTape()
    first_provider = FakeProvider(composite_response())
    first = compile_natural_language(request(), router(first_provider, tape=tape))

    replay_provider = FakeProvider("should not be requested")
    replay_router = router(replay_provider, mode=LLMMode.REPLAY, tape=tape)
    replay = compile_natural_language(request(), replay_router)

    assert first.draft_plan is not None and replay.draft_plan is not None
    assert replay.draft_plan.model_dump(mode="json") == first.draft_plan.model_dump(mode="json")
    assert replay_provider.calls == []
    assert replay_router.api_request_count == 0
    assert replay.usage.cache_hit is True


def test_invalid_target_pair_gets_at_most_one_semantic_repair() -> None:
    invalid = json.loads(composite_response())
    invalid["support_mode"] = "native"
    invalid["effects"] = [
        {
            "op": "water_outage",
            "target_kind": "facility",
            "target_id": "toilet-1",
            "start_minute": 3000,
            "end_minute": 3120,
            "parameters": {"outage": True},
            "assumption_indexes": [],
        }
    ]
    provider = FakeProvider(json.dumps(invalid), composite_response())
    active_router = router(provider)

    result = compile_natural_language(request(), active_router)

    assert result.status == "compiled"
    assert len(provider.calls) == 2
    assert provider.calls[0][1].prompt_version.endswith("v2")
    assert provider.calls[1][1].prompt_version.endswith("repair1")
    assert result.usage.api_request_count == 2


def test_offline_known_form_is_labeled_fallback_but_free_text_is_not_faked() -> None:
    offline_router = LLMRouter(LLMSettings(mode=LLMMode.OFFLINE))
    known = compile_natural_language(
        request("Day 3 toilet 1 outage"),
        offline_router,
    )
    unknown = compile_natural_language(
        request("子どもが泣き続け、避難者が眠れない"),
        LLMRouter(LLMSettings(mode=LLMMode.OFFLINE)),
    )

    assert known.status == "compiled"
    assert known.fallback_used is True
    assert known.draft_plan is not None
    assert known.draft_plan.compiler_provenance.compiler_kind == "offline_rule"
    assert known.usage.api_request_count == 0
    assert unknown.status == "not_simulatable"
    assert unknown.draft_plan is None
    assert unknown.support_mode == "not_simulatable"
    assert unknown.fallback_used is True
    assert unknown.limitations


def test_offline_fallback_rejects_partially_understood_multi_clause_request() -> None:
    result = compile_natural_language(
        request("Day 3 toilet 1 outage and infection spreads"),
        LLMRouter(LLMSettings(mode=LLMMode.OFFLINE)),
    )

    assert result.status == "not_simulatable"
    assert result.draft_plan is None
    assert result.support_mode == "not_simulatable"
    assert "partial plan was discarded" in result.limitations[0]


def test_missing_policy_is_not_advertised_or_returned_as_success() -> None:
    provider = FakeProvider(composite_response())
    no_water = request().model_copy(update={"scenario_summary": summary(with_water_policy=False)})
    result = compile_natural_language(no_water, router(provider))

    assert result.status == "not_simulatable"
    assert result.support_mode == "not_simulatable"
    assert result.draft_plan is None
    assert provider.calls == []
    assert "有効policy" in result.limitations[0]


def test_rate_limit_uses_recognized_offline_plan_and_reports_failure() -> None:
    provider = FakeProvider(error=LLMProviderError("rate limited", status_code=429))
    result = compile_natural_language(
        request("Day 3 toilet 1 outage"),
        router(provider),
    )

    assert result.status == "compiled"
    assert result.fallback_used is True
    assert result.draft_plan is not None
    assert result.draft_plan.compiler_provenance.compiler_kind == "offline_rule"
    assert result.usage.error_kind == "rate_limited"
    assert result.usage.api_request_count == 1


def test_budget_exhaustion_uses_explicit_offline_fallback_without_provider_call() -> None:
    provider = FakeProvider("should not be requested")
    active_router = LLMRouter(
        LLMSettings(
            mode=LLMMode.RECORD,
            api_key="test-key",
            budget_usd=0.0,
            max_api_calls=2,
            max_input_tokens=16_384,
            max_output_tokens=4096,
        ),
        provider=provider,
    )

    result = compile_natural_language(request("Day 3 toilet 1 outage"), active_router)

    assert result.status == "compiled"
    assert result.fallback_used is True
    assert result.usage.error_kind == "budget_exceeded"
    assert result.usage.api_request_count == 0
    assert provider.calls == []


def test_prompt_redacts_contact_details_and_only_sends_anonymous_context() -> None:
    provider = FakeProvider(not_simulatable_response())
    prompt_request = request(
        "Day 3 03:00 contact 090-1234-5678 private@example.com; water service issue"
    )

    result = compile_natural_language(prompt_request, router(provider))

    assert result.status == "not_simulatable"
    payload = json.loads(provider.calls[0][1].input_text)
    narrative = payload["untrusted_narrative"]
    assert "090-1234-5678" not in narrative
    assert "private@example.com" not in narrative
    assert "[redacted-phone]" in narrative
    assert "[redacted-email]" in narrative
    assert "A001" not in provider.calls[0][1].input_text


def test_invalid_model_target_is_rejected_without_mutating_world_state() -> None:
    from sheltersim.core.world import WorldState
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.llm.natural_language import scenario_summary_from_world

    scenario = ScenarioSpec.load(FIXTURE)
    world = WorldState.from_scenario(scenario)
    before = world.canonical_json()
    context = scenario_summary_from_world(world)
    invalid = json.dumps(
        {
            "status": "compiled",
            "support_mode": "native",
            "mechanism_summary": "Pretend facility failure.",
            "effects": [
                {
                    "op": "facility_outage",
                    "target_kind": "facility",
                    "target_id": "unknown-facility",
                    "start_minute": 5160,
                    "parameters": {"units": 1},
                    "assumption_indexes": [],
                }
            ],
            "assumptions": [],
            "limitations": [],
            "questions": [],
            "choices": [],
        }
    )
    provider = FakeProvider(invalid)
    invalid_request = ParseRequest(
        text="Day4 toilet unknown-facility failure",
        branch_id="baseline",
        selected_minute=world.minute_index,
        scenario_summary=context,
    )

    result = compile_natural_language(invalid_request, router(provider))

    assert result.status == "not_simulatable"
    assert result.draft_plan is None
    assert result.usage.error_kind == "schema_validation_failed"
    assert world.canonical_json() == before


def test_rule_fallback_results_say_why_opus_was_not_used() -> None:
    offline = compile_natural_language(
        request("Day 3 toilet 1 outage"), LLMRouter(LLMSettings(mode=LLMMode.OFFLINE))
    )
    capped = compile_natural_language(
        request("Day 3 toilet 1 outage"),
        LLMRouter(
            LLMSettings(mode=LLMMode.RECORD, api_key="test-key", budget_usd=0.0),
            provider=FakeProvider("should not be requested"),
        ),
    )
    unknown = compile_natural_language(
        request("子どもが泣き続け、避難者が眠れない"),
        LLMRouter(LLMSettings(mode=LLMMode.OFFLINE)),
    )

    assert offline.status == "compiled"
    assert "offline mode" in offline.limitations[-1]
    assert "（理由コード: offline）" in offline.limitations[-1]
    assert capped.status == "compiled"
    assert "予算の上限" in capped.limitations[-1]
    assert "（理由コード: budget_exceeded）" in capped.limitations[-1]
    assert unknown.status == "not_simulatable"
    assert "offline mode" in unknown.limitations[0]


def stop_router(provider: FakeProvider) -> LLMRouter:
    active = router(provider)
    active.settings = replace(active.settings, error_policy=LLMErrorPolicy.STOP)
    return active


def test_stop_policy_reports_the_failure_instead_of_a_rule_plan() -> None:
    rate_limited = FakeProvider(error=LLMProviderError("rate limited", status_code=429))
    with pytest.raises(InterventionCompilerUnavailable, match="429"):
        compile_natural_language(request("Day 3 toilet 1 outage"), stop_router(rate_limited))

    invalid = json.loads(composite_response())
    invalid["support_mode"] = "native"
    invalid["effects"] = [
        {
            "op": "water_outage",
            "target_kind": "facility",
            "target_id": "toilet-1",
            "start_minute": 3000,
            "end_minute": 3120,
            "parameters": {"outage": True},
            "assumption_indexes": [],
        }
    ]
    rejected = FakeProvider(json.dumps(invalid), json.dumps(invalid))
    with pytest.raises(InterventionCompilerUnavailable, match="schema/domain"):
        compile_natural_language(request("Day 3 toilet 1 outage"), stop_router(rejected))
    assert len(rejected.calls) == 2  # the one repair still runs before the failure


def test_stop_policy_keeps_rule_results_in_explicit_offline_mode() -> None:
    result = compile_natural_language(
        request("Day 3 toilet 1 outage"),
        LLMRouter(LLMSettings(mode=LLMMode.OFFLINE, error_policy=LLMErrorPolicy.STOP)),
    )

    assert result.status == "compiled"
    assert result.fallback_used is True


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


def parser_summary(*, toilets: int = 1, default_zone_id: str | None = "main") -> ScenarioSummary:
    facilities = [
        FacilityTarget(
            id=f"toilet-{index}",
            name=f"Restroom {index}",
            kind="toilet",
            zone_id="main",
            capacity=4,
            working_units=4,
        )
        for index in range(1, toilets + 1)
    ]
    facilities.append(
        FacilityTarget(
            id="water-station",
            name="Water station",
            kind="water_station",
            zone_id="main",
            capacity=1,
            working_units=1,
        )
    )
    return ScenarioSummary(
        duration_minutes=10080,
        start_date=date(2026, 10, 12),
        default_zone_id=default_zone_id,
        facilities=facilities,
        zones=[
            ZoneTarget(id="main", name="Main hall", has_water_station=True),
            ZoneTarget(id="sleeping", name="Sleeping area"),
        ],
    )


def parse(text: str, *, context: ScenarioSummary | None = None):
    return parse_natural_language(
        ParseRequest(
            text=text,
            branch_id="baseline",
            selected_minute=0,
            scenario_summary=context or parser_summary(),
        )
    )


@pytest.mark.parametrize(
    ("text", "op", "minute", "count"),
    [
        ("Day4 14:00にトイレ2基故障", "facility_outage", 5160, 2),
        ("4日目14時にトイレ二基が故障", "facility_outage", 5160, 2),
        ("4日目午後2時に便器1台が壊れた", "facility_outage", 5160, 1),
        ("Day 4 at 2pm toilet 2 fail", "facility_outage", 5160, 2),
        ("Day 4 staff 2 add", "add_staff", 4320, 2),
        ("4日目にスタッフを2人追加", "add_staff", 4320, 2),
        ("4日目に職員二名増員", "add_staff", 4320, 2),
        ("Day4 add 2 toilets", "add_toilet", 4320, 2),
        ("4日目に仮設トイレを2基追加", "add_toilet", 4320, 2),
        ("4日目にトイレが使えなくなる", "facility_outage", 4320, 1),
        ("4日目にトイレ二基の利用を停止", "facility_outage", 4320, 2),
        ("4日目スタッフ二人追加", "add_staff", 4320, 2),
        ("4日目にスタッフを二人増やす", "add_staff", 4320, 2),
        ("Day4 water outage", "water_outage", 4320, None),
        ("4日目に断水", "water_outage", 4320, None),
        ("4日目の給水が停止", "water_outage", 4320, None),
    ],
)
def test_offline_parser_handles_japanese_and_english_examples(
    text: str, op: str, minute: int, count: int | None
) -> None:
    result = parse(text)

    assert result.status == "parsed"
    assert result.issues == []
    assert result.draft_plan is not None
    action = result.draft_plan.effects[0]
    assert action.op == op
    assert action.start_minute == minute
    assert action.source_id == "offline-parser-h06"
    assert action.target_selector.target_id is not None
    if count is not None:
        assert action.parameters["count" if op != "facility_outage" else "units"] == count


def test_day_four_1400_converts_to_5160_and_keeps_existing_target_id() -> None:
    result = parse("Day4 14:00にトイレ2基故障")
    assert result.draft_plan is not None
    assert result.draft_plan.effects[0].start_minute == 5160
    assert result.draft_plan.effects[0].target_selector.target_id == "toilet-1"
    assert result.draft_plan.effects[0].target_selector.target_id != "A001"


def test_composed_plan_with_two_effect_types_passes_shared_schema() -> None:
    result = parse("Day4 toilet 2 failure and staff 2 added")

    assert result.status == "parsed"
    assert result.draft_plan is not None
    assert result.draft_plan.support_mode == "composed"
    assert {item.op for item in result.draft_plan.effects} == {"facility_outage", "add_staff"}
    decoded = DraftPlan.model_validate_json(result.draft_plan.model_dump_json())
    assert len(decoded.effects) == 2
    assert all(item.units and item.start_minute == 4320 for item in decoded.effects)


def test_schema_accepts_a_new_typed_operator_without_allowlisting_its_name() -> None:
    plan = parse("Day4 toilet 1 failure").draft_plan
    assert plan is not None
    document = plan.model_dump(
        mode="json",
        exclude={"branch_id", "fork_at_minute", "ambiguities", "requires_confirmation"},
    )
    document["effects"][0]["op"] = "future_supported_primitive"

    program = EffectProgram.model_validate(document)

    assert program.effects[0].op == "future_supported_primitive"


def test_guard_is_a_bounded_typed_ast_with_an_allowlisted_field_set() -> None:
    plan = parse("Day4 toilet 1 failure").draft_plan
    assert plan is not None
    document = plan.model_dump(mode="json")
    leaf = {"operator": "gte", "field": "target.working_units", "value": 2}
    document["effects"][0]["guard"] = {
        "operator": "and",
        "operands": [
            leaf,
            {"operator": "lt", "field": "simulation.minute", "value": 9000},
        ],
    }

    guarded = DraftPlan.model_validate(document)
    assert guarded.effects[0].guard is not None

    document["effects"][0]["guard"] = {
        "operator": "eq",
        "field": "world.stress",
        "value": 1,
    }
    with pytest.raises(ValidationError, match="world.stress"):
        DraftPlan.model_validate(document)


def test_guard_rejects_excessive_nesting() -> None:
    plan = parse("Day4 toilet 1 failure").draft_plan
    assert plan is not None
    document = plan.model_dump(mode="json")
    leaf = {"operator": "eq", "field": "simulation.minute", "value": 1}
    guard = leaf
    for _ in range(4):
        guard = {"operator": "and", "operands": [guard, leaf]}
    document["effects"][0]["guard"] = guard

    with pytest.raises(ValidationError, match="depth cannot exceed four"):
        DraftPlan.model_validate(document)


def test_explicit_date_and_time_range_are_converted_with_scenario_anchor() -> None:
    result = parse("2026-10-14 14:00から16:00まで toilet outage")

    assert result.draft_plan is not None
    effect = result.draft_plan.effects[0]
    assert (effect.start_minute, effect.end_minute) == (3720, 3840)


def test_ambiguous_target_returns_candidates_for_confirmation() -> None:
    result = parse("Day4 toilet 2 failure", context=parser_summary(toilets=2))

    assert result.status == "clarification_required"
    assert result.draft_plan is not None
    assert result.draft_plan.support_mode == "clarification_required"
    assert result.draft_plan.requires_confirmation is True
    assert result.draft_plan.effects[0].target_selector.target_id is None
    assert result.draft_plan.effects[0].target_selector.candidate_ids == ["toilet-1", "toilet-2"]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("午後にトイレが故障", "ambiguous_time"),
        ("Day4 staff -2 add", "invalid_count"),
        ("Day4 staff 1000 add", "invalid_count"),
        ("Day4 toilet 5 fail", "invalid_count"),
        ("感染者を1000人増やして", "unsupported_event"),
    ],
)
def test_unsafe_or_unsupported_input_is_rejected(text: str, code: str) -> None:
    result = parse(text)

    assert result.status == "rejected"
    assert result.draft_plan is None
    assert code in {item.code for item in result.issues}


def test_unknown_target_is_rejected_instead_of_inventing_a_facility() -> None:
    context = ScenarioSummary(
        duration_minutes=10080,
        zones=[ZoneTarget(id="main", name="Main hall")],
    )
    result = parse("Day4 toilet 1 failure", context=context)

    assert result.status == "rejected"
    assert result.draft_plan is None
    assert result.issues[0].code == "unknown_target"


def test_explicit_unknown_target_id_is_not_replaced_by_the_only_matching_kind() -> None:
    result = parse("Day4 toilet missing-restroom failure")

    assert result.status == "rejected"
    assert result.draft_plan is None
    assert result.issues[0].code == "unknown_target"
    assert result.issues[0].choices == ["toilet-1"]


def test_parser_does_not_mutate_world_rng_event_queue_or_branch_runtime() -> None:
    scenario = ScenarioSpec.load(FIXTURE)
    world = WorldState.from_scenario(scenario)
    runtime = BranchRuntime.from_world(world)
    original_world = world.canonical_json()
    runtime_world = runtime.world
    original_runtime_world = runtime.world.canonical_json()
    original_branch_id = runtime.branch_id
    original_parent_id = runtime.parent_branch_id
    original_command_log = list(runtime.command_log)

    scenario_summary = ScenarioSummary(
        duration_minutes=scenario.duration_minutes,
        facilities=[
            FacilityTarget(
                id=item.id,
                name=item.id,
                kind=item.kind,
                zone_id=item.zone_id,
                capacity=max(1, item.capacity),
                working_units=min(
                    item.capacity if item.working_units is None else item.working_units,
                    max(1, item.capacity),
                ),
            )
            for item in scenario.facilities
            if item.kind == "toilet"
        ],
        zones=[ZoneTarget(id=item.id, name=item.name) for item in scenario.zones],
        default_zone_id=scenario.zones[0].id,
    )
    result = parse_natural_language(
        ParseRequest(
            text="Day4 14:00 toilet_1 failure",
            branch_id=runtime.branch_id,
            selected_minute=world.minute_index,
            scenario_summary=scenario_summary,
        )
    )

    assert result.status == "parsed"
    assert world.canonical_json() == original_world
    assert runtime.world is runtime_world
    assert runtime.world.canonical_json() == original_runtime_world
    assert runtime.branch_id == original_branch_id
    assert runtime.parent_branch_id == original_parent_id
    assert runtime.command_log == original_command_log


def test_shared_contract_rejects_unknown_fields_and_invalid_time_windows() -> None:
    result = parse("Day4 toilet 1 failure")
    assert result.draft_plan is not None
    document = result.draft_plan.model_dump(mode="json")
    document["effects"][0]["unexpected"] = True
    with pytest.raises(ValidationError, match="unexpected"):
        DraftPlan.model_validate(document)

    document = result.draft_plan.model_dump(mode="json")
    document["effects"][0]["end_minute"] = document["effects"][0]["start_minute"]
    with pytest.raises(ValidationError, match="later than"):
        DraftPlan.model_validate(document)


def test_hyphenated_facility_id_is_a_target_not_a_negative_count() -> None:
    result = parse("Day4 toilet-2を故障させる", context=parser_summary(toilets=2))

    assert result.status == "parsed", result.issues
    assert result.draft_plan is not None
    effect = result.draft_plan.effects[0]
    assert effect.op == "facility_outage"
    assert effect.target_selector.target_id == "toilet-2"
    assert effect.parameters == {"units": 1}


def test_out_of_range_outage_count_names_units_not_evacuees() -> None:
    result = parse("Day4 トイレが2000基故障")

    assert result.status == "rejected"
    message = next(item.message for item in result.issues if item.code == "invalid_count")
    assert "停止するユニット" in message
    assert "避難者" not in message


def parse_target(text):
    return parse_natural_language(
        ParseRequest(
            text=text,
            branch_id="main",
            selected_minute=5,
            scenario_summary=ScenarioSummary(
                duration_minutes=60,
                zones=[
                    ZoneTarget(id="toilet", name="トイレ"),
                    ZoneTarget(id="staff", name="スタッフ"),
                    ZoneTarget(id="sleeping", name="避難スペース"),
                ],
            ),
        )
    )


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("トイレを2基追加する zone_id: toilet", "toilet"),
        ("スタッフを2人追加する zone_id: staff", "staff"),
        ("避難スペースではなく zone_id: toilet にトイレを2基追加", "toilet"),
    ],
)
def test_explicit_existing_zone_overrides_ambiguous_common_words(text, target):
    result = parse_target(text)
    assert result.status == "parsed"
    assert result.draft_plan.effects[0].target_selector.target_id == target
    assert result.draft_plan.effects[0].start_minute == 5


def test_unknown_zone_stays_rejected_and_unqualified_text_requires_choice():
    unknown = parse_target("トイレを2基追加する zone_id: missing")
    assert unknown.status == "rejected"
    assert unknown.issues[0].code == "unknown_target"
    ambiguous = parse_target("トイレを2基追加する")
    assert ambiguous.status == "clarification_required"
