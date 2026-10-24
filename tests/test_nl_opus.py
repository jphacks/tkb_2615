"""Offline coverage for the Opus Event Compiler and its truthful fallback contract."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings, ModelPrice
from sheltersim.llm.contracts import LLMProviderError, LLMStage, ProviderResponse
from sheltersim.llm.natural_language import compile_natural_language
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.schemas import FacilityTarget, ParseRequest, ScenarioSummary, ZoneTarget

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


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
