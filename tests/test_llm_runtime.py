"""Bounded Haiku social triggers, typed events, replay, and decision audit tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.domain.models import SupportRequestState
from sheltersim.domain.staff import StaffService
from sheltersim.events.handlers import EventDispatcher, SocialInteractionHandler
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings, ModelPrice
from sheltersim.llm.contracts import ProviderResponse
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.runtime import EventTriggeredSocialPolicy, SocialRuntimeLimits

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


class FakeProvider:
    def __init__(self, action: str = "offer_reassurance") -> None:
        self.action = action
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        del max_output_tokens, timeout_seconds
        self.calls.append((model_id, task.input_text))
        return ProviderResponse(
            text=json.dumps({"action": self.action}),
            model_id=model_id,
            input_tokens=50,
            output_tokens=5,
            elapsed_ms=8.0,
        )


def scenario() -> ScenarioSpec:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["duration_minutes"] = 600
    document["scheduled_events"] = []
    document["profiles"] = document["profiles"][:3]
    return ScenarioSpec.model_validate(document)


def active_request(world: WorldState, agent_id: str = "A001") -> str:
    world.minute_index = 60
    agent = world.agents[agent_id]
    agent.stress = 0.8
    agent.trust = 0.2
    receipt = StaffService(world).request(agent_id, minute=60)
    return receipt.request_id


def make_router(
    action: str = "offer_reassurance",
    *,
    mode: LLMMode = LLMMode.RECORD,
    tape: ResponseTape | None = None,
) -> LLMRouter:
    settings = LLMSettings(
        mode=mode,
        api_key="test-key" if mode is LLMMode.RECORD else None,
        max_api_calls=4,
        max_input_tokens=4096,
        max_output_tokens=64,
        budget_usd=0.1,
        opus_price=ModelPrice(1, 2),
        haiku_price=ModelPrice(0.1, 0.5),
    )
    return LLMRouter(settings, provider=FakeProvider(action), tape=tape or ResponseTape())


def test_social_request_creates_one_typed_future_event_and_applies_bounded_effect() -> None:
    world = WorldState.from_scenario(scenario())
    request_id = active_request(world)
    decisions = []
    router = make_router()
    policy = EventTriggeredSocialPolicy(router, record_decision=decisions.append)

    decision = policy.consider_support_request(world, agent_id="A001", request_id=request_id)

    assert decision.accepted is True
    assert decision.event_id == "social-interaction-1"
    event = next(
        event for event in world.event_queue.pending() if event.event_type == "social_interaction"
    )
    assert event.due_minute == 61
    assert event.causation_id == request_id
    assert event.provenance["source_type"] == "assumption"
    assert event.provenance["source_id"] == "ASM-EVENT-TRIGGERED-SOCIAL-LIMITS-V1"
    assert event.payload["action"] == "offer_reassurance"
    assert world.agents["A001"].stress == 0.8
    assert len(router.provider.calls) == 1
    assert "A001" not in router.provider.calls[0][1]
    assert decisions[0]["accepted"] is True
    assert decisions[0]["model_id"] == router.settings.haiku_model
    assert decisions[0]["source_id"] == "ASM-EVENT-TRIGGERED-SOCIAL-LIMITS-V1"

    world.minute_index = 61
    EventDispatcher({"social_interaction": SocialInteractionHandler()}).dispatch_due(world, 61)
    assert world.agents["A001"].stress == 0.77
    assert world.agents["A001"].trust == pytest.approx(0.21)
    assert world.event_log.records[-1].status.value == "applied"
    world.validate()


def test_social_gate_and_daily_caps_prevent_repeated_or_unselected_calls() -> None:
    world = WorldState.from_scenario(scenario())
    router = make_router()
    decisions = []
    policy = EventTriggeredSocialPolicy(
        router,
        record_decision=decisions.append,
        limits=SocialRuntimeLimits(global_per_day=1, per_agent_per_day=2, cooldown_minutes=0),
    )
    first_request = active_request(world)
    first = policy.consider_support_request(world, agent_id="A001", request_id=first_request)
    second_request = "support-second"
    world.support_requests[second_request] = SupportRequestState(
        id=second_request,
        agent_id="A002",
        requested_minute=60,
        timeout_minute=150,
    )
    world.agents["A002"].stress = 0.9
    second = policy.consider_support_request(world, agent_id="A002", request_id=second_request)

    assert first.accepted is True
    assert second.reason == "global_daily_cap"
    assert len(router.provider.calls) == 1

    low_stress_request = "support-low-stress"
    world.support_requests[low_stress_request] = SupportRequestState(
        id=low_stress_request,
        agent_id="A003",
        requested_minute=60,
        timeout_minute=150,
    )
    world.agents["A003"].stress = 0.1
    low_stress = policy.consider_support_request(
        world, agent_id="A003", request_id=low_stress_request
    )
    assert low_stress.reason == "below_stress_threshold"
    assert len(router.provider.calls) == 1
    assert [item["accepted"] for item in decisions] == [True, False, False]


def test_invalid_model_action_records_rejection_and_uses_rule_fallback() -> None:
    world = WorldState.from_scenario(scenario())
    request_id = active_request(world)
    decisions = []
    router = make_router("invented_action")
    policy = EventTriggeredSocialPolicy(router, record_decision=decisions.append)

    decision = policy.consider_support_request(world, agent_id="A001", request_id=request_id)

    assert decision.accepted is False
    assert decision.action == "no_interaction"
    assert decision.reason == "rule_fallback:invalid_output"
    assert not any(
        event.event_type == "social_interaction" for event in world.event_queue.pending()
    )
    assert router.records[0].error_kind == "schema_validation_failed"
    assert decisions[0]["fallback_used"] is True


def test_agent_cooldown_allows_a_later_same_day_trigger_after_its_window() -> None:
    world = WorldState.from_scenario(scenario())
    request_id = active_request(world)
    router = make_router()
    policy = EventTriggeredSocialPolicy(
        router,
        record_decision=lambda _: None,
        limits=SocialRuntimeLimits(global_per_day=4, per_agent_per_day=3, cooldown_minutes=240),
    )
    first = policy.consider_support_request(world, agent_id="A001", request_id=request_id)
    world.minute_index = 120
    too_soon = policy.consider_support_request(world, agent_id="A001", request_id=request_id)
    world.minute_index = 300
    later = policy.consider_support_request(world, agent_id="A001", request_id=request_id)

    assert first.accepted is True
    assert too_soon.reason == "cooldown"
    assert later.accepted is True
    assert len(router.provider.calls) == 2


def test_changed_branch_state_hash_does_not_reuse_a_cached_social_decision() -> None:
    base = WorldState.from_scenario(scenario())
    request_id = active_request(base)
    pristine = WorldState.from_json(base.canonical_json())
    recorded_tape = ResponseTape()
    recording_router = make_router(tape=recorded_tape)
    EventTriggeredSocialPolicy(
        recording_router, record_decision=lambda _: None
    ).consider_support_request(base, agent_id="A001", request_id=request_id)
    changed = WorldState.from_json(pristine.canonical_json())
    changed.agents["A001"].trust = 0.21
    replay_router = make_router(mode=LLMMode.REPLAY, tape=recorded_tape)

    decision = EventTriggeredSocialPolicy(
        replay_router, record_decision=lambda _: None
    ).consider_support_request(changed, agent_id="A001", request_id=request_id)

    assert decision.accepted is False
    assert decision.reason == "rule_fallback:replay_miss"
    assert replay_router.api_request_count == 0
    assert replay_router.records[0].outcome == "replay_miss"


def test_checkpoint_replay_and_noop_continuations_are_canonically_identical() -> None:
    world = WorldState.from_scenario(scenario())
    request_id = active_request(world)
    checkpoint_before_decision = Checkpoint.capture(world)
    recorded_world = checkpoint_before_decision.restore_world()
    tape = ResponseTape()
    recorder = make_router(tape=tape)
    EventTriggeredSocialPolicy(recorder, record_decision=lambda _: None).consider_support_request(
        recorded_world, agent_id="A001", request_id=request_id
    )
    checkpoint_with_tape = Checkpoint.capture(
        world,
        runtime_state={"llm_router": recorder.snapshot_state()},
        model_versions={"social": EventTriggeredSocialPolicy.policy_version},
    )

    restored_worlds = [
        checkpoint_with_tape.restore_world(),
        checkpoint_with_tape.restore_world(),
    ]
    replay_routers = [make_router(mode=LLMMode.REPLAY), make_router(mode=LLMMode.REPLAY)]
    for replay_router in replay_routers:
        replay_router.restore_snapshot(checkpoint_with_tape.runtime_state["llm_router"])
    for restored, replay_router in zip(restored_worlds, replay_routers, strict=True):
        assert restored.agents["A001"].support_request_id == request_id
        decision = EventTriggeredSocialPolicy(
            replay_router, record_decision=lambda _: None
        ).consider_support_request(restored, agent_id="A001", request_id=request_id)
        assert decision.accepted is True
        assert decision.cache_hit is True
    for restored in restored_worlds:
        social_event = next(
            event
            for event in restored.event_queue.pending()
            if event.event_type == "social_interaction"
        )
        due = social_event.due_minute
        restored.minute_index = due
        EventDispatcher({"social_interaction": SocialInteractionHandler()}).dispatch_due(
            restored, due
        )

    assert restored_worlds[0].canonical_json() == restored_worlds[1].canonical_json()
    assert all(router.api_request_count == 0 for router in replay_routers)
    assert all(router.records[-1].cache_hit for router in replay_routers)


def test_social_acceptance_and_llm_usage_are_saved_in_separate_artifacts(tmp_path: Path) -> None:
    selected_scenario = scenario()
    world = WorldState.from_scenario(selected_scenario)
    artifacts = RunArtifactWriter(selected_scenario, tmp_path / "run")
    artifacts.write_initial(world, execution_mode="record")
    request_id = active_request(world)
    router = make_router()
    router.observer = artifacts.record_llm_call
    policy = EventTriggeredSocialPolicy(router, record_decision=artifacts.record_social_decision)

    policy.consider_support_request(world, agent_id="A001", request_id=request_id)
    artifacts.finalize(world)

    social_record = json.loads(
        (tmp_path / "run" / "social_decisions.jsonl").read_text(encoding="utf-8")
    )
    llm_record = json.loads((tmp_path / "run" / "llm_calls.jsonl").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    assert social_record["accepted"] is True
    assert social_record["action"] == "offer_reassurance"
    assert social_record["input_hash"] == llm_record["input_hash"]
    assert "prompt" not in social_record
    assert manifest["social_decision_count"] == 1
    assert manifest["llm_usage"]["api_request_count"] == 1
