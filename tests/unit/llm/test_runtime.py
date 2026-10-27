"""Rule-filtered Haiku interaction proposals and their bounded event effects, with the
triggers, typed events, replay and decision audit of the social runtime.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.models import (
    AgentInformationState,
    InformationBeliefStatus,
    InformationSpreadConfig,
    InformationTruthStatus,
    SupportRequestState,
    TypedMessage,
)
from sheltersim.domain.staff import StaffService
from sheltersim.events.handlers import EventDispatcher, SocialInteractionHandler
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings, ModelPrice
from sheltersim.llm.contracts import ProviderResponse
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.runtime import EventTriggeredSocialPolicy, SocialRuntimeLimits


class TypedProvider:
    def __init__(
        self,
        action: str,
        *,
        event_id: str | None = None,
        reason_short: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.action = action
        self.event_id = event_id
        self.reason_short = reason_short
        self.extra = extra or {}
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        del max_output_tokens, timeout_seconds
        self.calls.append((model_id, task.input_text))
        schema = task.output_schema
        assert schema is not None
        event_id = self.event_id or schema["properties"]["event_id"]["enum"][0]
        reason_codes = schema["properties"]["reason_short"]["enum"]
        document = {
            "event_id": event_id,
            "action": self.action,
            "reason_short": self.reason_short or reason_codes[0],
            **self.extra,
        }
        return ProviderResponse(
            text=json.dumps(document),
            model_id=model_id,
            input_tokens=50,
            output_tokens=8,
            elapsed_ms=4.0,
        )


def scenario(*, information: bool = False) -> ScenarioSpec:
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["duration_minutes"] = 600
    document["scheduled_events"] = []
    document["profiles"] = document["profiles"][:4]
    if information:
        document["model_configs"]["information_spread"] = InformationSpreadConfig(
            enabled=True
        ).to_dict()
    return ScenarioSpec.model_validate(document)


def make_router(
    action: str,
    *,
    mode: LLMMode = LLMMode.RECORD,
    tape: ResponseTape | None = None,
    event_id: str | None = None,
    reason_short: str | None = None,
    extra: dict[str, Any] | None = None,
) -> LLMRouter:
    settings = LLMSettings(
        mode=mode,
        api_key="test-key" if mode is LLMMode.RECORD else None,
        max_api_calls=8,
        max_input_tokens=4096,
        max_output_tokens=64,
        budget_usd=0.1,
        opus_price=ModelPrice(1, 2),
        haiku_price=ModelPrice(0.1, 0.5),
    )
    return LLMRouter(
        settings,
        provider=TypedProvider(
            action,
            event_id=event_id,
            reason_short=reason_short,
            extra=extra,
        ),
        tape=tape or ResponseTape(),
    )


def pending_request(world: WorldState, *, minute: int = 0, agent_id: str = "A001") -> str:
    world.minute_index = minute
    agent = world.agents[agent_id]
    agent.stress = 0.8
    return StaffService(world).request(agent_id, minute=minute).request_id


def apply_due_social_event(world: WorldState, event_id: str) -> EventLogRecord:
    event = next(item for item in world.event_queue.pending() if item.event_id == event_id)
    world.minute_index = event.due_minute
    EventDispatcher({"social_interaction": SocialInteractionHandler()}).dispatch_due(
        world, event.due_minute
    )
    return world.event_log.records[-1]


def record_applied_contact(
    world: WorldState,
    *,
    event_id: str,
    sender_id: str,
    recipient_id: str,
    minute: int,
) -> None:
    sequence = world.allocate_event_sequence()
    contact = ScheduledEvent(
        event_id=event_id,
        kind=EventKind.ENDOGENOUS,
        due_minute=minute,
        priority=40,
        sequence=sequence,
        event_type="information_contact_update",
        payload={"sender_agent_id": sender_id, "recipient_agent_id": recipient_id},
    )
    world.event_log.append(EventLogRecord(contact, minute, EventStatus.APPLIED))


def test_long_wait_complaint_is_a_candidate_bound_typed_event_with_audited_effect() -> None:
    world = WorldState.from_scenario(scenario())
    request_id = pending_request(world)
    world.minute_index = 30
    router = make_router("explain_staff_process")
    decisions = []
    policy = EventTriggeredSocialPolicy(router, record_decision=decisions.append)

    decision = policy.consider_complaint_response(world, agent_id="A001", request_id=request_id)

    assert decision.accepted is True
    assert decision.reason_short == "long_wait"
    assert decision.effect_code == "requester_stress_relief_and_trust_increase"
    assert decision.estimated_cost_usd > 0
    prompt = router.provider.calls[0][1]
    assert "A001" in prompt and "wait_minutes" in prompt
    event = next(item for item in world.event_queue.pending() if item.event_id == decision.event_id)
    assert event.payload["interaction_type"] == "complaint_response"
    assert event.payload["reason_short"] == "long_wait"
    context = event.payload["conversation_state_context"]
    assert context["schema_version"] == 1
    assert context["captured_minute"] == world.minute_index
    assert context["participants"]["A001"]["stress"] == 0.8

    restored = Checkpoint.capture(world).restore_world()
    restored_event = next(
        item for item in restored.event_queue.pending() if item.event_id == event.event_id
    )
    assert restored_event.payload["conversation_state_context"] == context

    outcome = apply_due_social_event(restored, event.event_id)

    assert outcome.status is EventStatus.APPLIED
    assert restored.agents["A001"].stress == pytest.approx(0.79)
    assert restored.agents["A001"].trust == pytest.approx(0.51)
    assert decisions[0]["estimated_cost_usd"] == decision.estimated_cost_usd
    assert decisions[0]["input_hash"] == router.records[0].input_hash


def test_unselected_complaint_and_ineligible_relationship_do_not_call_provider() -> None:
    world = WorldState.from_scenario(scenario())
    request_id = pending_request(world)
    router = make_router("acknowledge_complaint")
    policy = EventTriggeredSocialPolicy(router, record_decision=lambda _: None)

    early = policy.consider_complaint_response(world, agent_id="A001", request_id=request_id)
    world.minute_index = 30
    world.agents["A001"].stress = 0.1
    low_stress = policy.consider_complaint_response(world, agent_id="A001", request_id=request_id)
    assert early.reason == "complaint_wait_threshold_not_met"
    assert low_stress.reason == "below_stress_threshold"

    world.agents["A002"].zone_id = next(
        zone_id for zone_id in world.zones if zone_id != world.agents["A001"].zone_id
    )
    outside_zone = policy.consider_cooperation(
        world,
        initiator_agent_id="A001",
        participant_agent_id="A002",
        trigger_event_id="contact-event-1",
    )
    assert outside_zone.reason == "participants_not_in_same_zone"
    assert router.api_request_count == 0
    assert router.provider.calls == []


def test_cooperation_revalidates_both_agents_zone_and_group_at_event_time() -> None:
    world = WorldState.from_scenario(scenario())
    world.minute_index = 20
    world.agents["A001"].stress = 0.8
    world.agents["A002"].stress = 0.7
    record_applied_contact(
        world,
        event_id="contact-event-1",
        sender_id="A001",
        recipient_id="A002",
        minute=20,
    )
    router = make_router("offer_mutual_support")
    policy = EventTriggeredSocialPolicy(router, record_decision=lambda _: None)

    decision = policy.consider_cooperation(
        world,
        initiator_agent_id="A001",
        participant_agent_id="A002",
        trigger_event_id="contact-event-1",
    )
    assert decision.accepted is True
    assert decision.target_agent_id == "A002"
    assert "A001" in router.provider.calls[0][1]
    assert "A002" in router.provider.calls[0][1]

    world.agents["A002"].zone_id = next(
        zone_id for zone_id in world.zones if zone_id != world.agents["A001"].zone_id
    )
    before = (world.agents["A001"].stress, world.agents["A002"].stress)
    outcome = apply_due_social_event(world, decision.event_id)
    assert outcome.status is EventStatus.SUPPRESSED
    assert (world.agents["A001"].stress, world.agents["A002"].stress) == before


def test_cooperation_suppresses_if_group_relationship_changes_before_application() -> None:
    world = WorldState.from_scenario(scenario())
    world.minute_index = 20
    world.agents["A001"].stress = 0.8
    world.agents["A002"].stress = 0.7
    record_applied_contact(
        world,
        event_id="contact-event-5",
        sender_id="A001",
        recipient_id="A002",
        minute=20,
    )
    decision = EventTriggeredSocialPolicy(
        make_router("offer_mutual_support"), record_decision=lambda _: None
    ).consider_cooperation(
        world,
        initiator_agent_id="A001",
        participant_agent_id="A002",
        trigger_event_id="contact-event-5",
    )
    world.agents["A002"].profile = replace(world.agents["A002"].profile, group_id="G099")

    outcome = apply_due_social_event(world, decision.event_id)

    assert outcome.status is EventStatus.SUPPRESSED
    assert world.agents["A001"].stress == pytest.approx(0.8)
    assert world.agents["A002"].stress == pytest.approx(0.7)


def test_information_reaction_records_verification_request_without_rewriting_belief(
    tmp_path: Path,
) -> None:
    selected_scenario = scenario(information=True)
    world = WorldState.from_scenario(selected_scenario)
    artifacts = RunArtifactWriter(selected_scenario, tmp_path / "run")
    artifacts.write_initial(world, execution_mode="record")
    world.minute_index = 10
    sender = world.agents["A001"]
    recipient = world.agents["A002"]
    claim = TypedMessage(
        id="claim-false-1",
        truth_status=InformationTruthStatus.FALSE,
        source_id="public-notice-1",
        zone_id=sender.zone_id,
        time=5,
        origin_event_id="rumor-origin-1",
        origin_agent_id=sender.id,
    )
    state = world.model_state[INFORMATION_SPREAD_STATE_KEY]
    state["messages"][claim.id] = claim.to_dict()
    state["beliefs"][recipient.id][claim.id] = AgentInformationState(
        message_id=claim.id,
        belief=InformationBeliefStatus.HEARD,
        source=sender.id,
        last_contact=10,
        last_event_id="contact-event-1",
    ).to_dict()
    contact_sequence = world.allocate_event_sequence()
    contact_event = ScheduledEvent(
        event_id="contact-event-1",
        kind=EventKind.ENDOGENOUS,
        due_minute=10,
        priority=40,
        sequence=contact_sequence,
        event_type="information_contact_update",
        payload={
            "message_id": claim.id,
            "sender_agent_id": sender.id,
            "recipient_agent_id": recipient.id,
            "belief": "heard",
            "contact_basis": "family",
            "contact_minutes": 5,
        },
    )
    contact_record = EventLogRecord(contact_event, 10, EventStatus.APPLIED)
    world.event_log.append(contact_record)
    router = make_router("seek_claim_verification")
    controller = OfflineSimulationController(world, artifacts, llm_router=router)

    controller._consider_social_contact_events((contact_record,))

    proposal_event = next(
        item for item in world.event_queue.pending() if item.event_type == "social_interaction"
    )
    prompt = router.provider.calls[0][1]
    assert "claim-false-1" not in prompt
    assert '"truth_status"' not in prompt
    assert '"false"' not in prompt
    outcome = apply_due_social_event(world, proposal_event.event_id)
    assert outcome.status is EventStatus.APPLIED
    assert state["beliefs"][recipient.id][claim.id]["belief"] == "heard"
    assert state["query_pressure"][recipient.id] == pytest.approx(0.5)
    assert state["support_requests"] == []
    assert state["counters"]["support_requests"] == 0


def test_proposal_schema_rejects_invented_action_id_and_extra_participant() -> None:
    world = WorldState.from_scenario(scenario())
    world.minute_index = 20
    world.agents["A001"].stress = 0.8
    world.agents["A002"].stress = 0.7
    record_applied_contact(
        world,
        event_id="contact-event-2",
        sender_id="A001",
        recipient_id="A002",
        minute=20,
    )
    router = make_router(
        "offer_mutual_support",
        event_id="social-interaction-invented",
        extra={"participant_agent_id": "A999"},
    )
    decision = EventTriggeredSocialPolicy(
        router, record_decision=lambda _: None
    ).consider_cooperation(
        world,
        initiator_agent_id="A001",
        participant_agent_id="A002",
        trigger_event_id="contact-event-2",
    )

    assert decision.accepted is False
    assert decision.fallback_used is True
    assert decision.reason == "rule_fallback:invalid_output"
    assert decision.event_id is None
    assert not any(item.event_type == "social_interaction" for item in world.event_queue.pending())


def test_proposal_schema_rejects_an_invented_action() -> None:
    world = WorldState.from_scenario(scenario())
    world.minute_index = 20
    world.agents["A001"].stress = 0.8
    world.agents["A002"].stress = 0.7
    record_applied_contact(
        world,
        event_id="contact-event-6",
        sender_id="A001",
        recipient_id="A002",
        minute=20,
    )
    router = make_router("invented_action")

    decision = EventTriggeredSocialPolicy(
        router, record_decision=lambda _: None
    ).consider_cooperation(
        world,
        initiator_agent_id="A001",
        participant_agent_id="A002",
        trigger_event_id="contact-event-6",
    )

    assert decision.accepted is False
    assert decision.reason == "rule_fallback:invalid_output"
    assert router.records[0].error_kind == "schema_validation_failed"


def test_no_interaction_records_choice_without_changing_agents_or_scheduling_event() -> None:
    world = WorldState.from_scenario(scenario())
    world.minute_index = 20
    world.agents["A001"].stress = 0.8
    world.agents["A002"].stress = 0.7
    record_applied_contact(
        world,
        event_id="contact-event-7",
        sender_id="A001",
        recipient_id="A002",
        minute=20,
    )
    before = {agent_id: (agent.stress, agent.trust) for agent_id, agent in world.agents.items()}
    pending_before = world.event_queue.pending()
    router = make_router("no_interaction")

    decision = EventTriggeredSocialPolicy(
        router, record_decision=lambda _: None
    ).consider_cooperation(
        world,
        initiator_agent_id="A001",
        participant_agent_id="A002",
        trigger_event_id="contact-event-7",
    )

    assert decision.accepted is True
    assert decision.event_id is None
    assert decision.effect_code is None
    assert world.event_queue.pending() == pending_before
    assert {
        agent_id: (agent.stress, agent.trust) for agent_id, agent in world.agents.items()
    } == before


def test_pair_candidate_charges_both_agents_and_replays_without_network() -> None:
    before = WorldState.from_scenario(scenario())
    before.minute_index = 20
    before.agents["A001"].stress = 0.8
    before.agents["A002"].stress = 0.7
    record_applied_contact(
        before,
        event_id="contact-event-3",
        sender_id="A001",
        recipient_id="A002",
        minute=20,
    )
    checkpoint_state = before.canonical_json()
    tape = ResponseTape()
    recording_router = make_router("offer_mutual_support", tape=tape)
    limits = SocialRuntimeLimits(per_agent_per_day=1, global_per_day=4, cooldown_minutes=0)
    first = EventTriggeredSocialPolicy(
        recording_router, record_decision=lambda _: None, limits=limits
    ).consider_cooperation(
        before,
        initiator_agent_id="A001",
        participant_agent_id="A002",
        trigger_event_id="contact-event-3",
    )
    replay_world = WorldState.from_json(checkpoint_state)
    replay_router = make_router("offer_mutual_support", mode=LLMMode.REPLAY, tape=tape)
    replay = EventTriggeredSocialPolicy(
        replay_router, record_decision=lambda _: None, limits=limits
    ).consider_cooperation(
        replay_world,
        initiator_agent_id="A001",
        participant_agent_id="A002",
        trigger_event_id="contact-event-3",
    )

    assert first.accepted is True and replay.accepted is True
    assert replay.cache_hit is True
    assert first.event_id == replay.event_id
    assert before.canonical_json() == replay_world.canonical_json()
    assert replay_router.api_request_count == 0

    changed_world = WorldState.from_json(checkpoint_state)
    changed_world.agents["A001"].trust = 0.3
    changed_router = make_router("offer_mutual_support", mode=LLMMode.REPLAY, tape=tape)
    changed = EventTriggeredSocialPolicy(
        changed_router, record_decision=lambda _: None, limits=limits
    ).consider_cooperation(
        changed_world,
        initiator_agent_id="A001",
        participant_agent_id="A002",
        trigger_event_id="contact-event-3",
    )
    assert changed.accepted is False
    assert changed.reason == "rule_fallback:replay_miss"
    assert changed_router.api_request_count == 0

    before.minute_index = 300
    before.agents["A003"].stress = 0.8
    before.agents["A002"].stress = 0.8
    record_applied_contact(
        before,
        event_id="contact-event-4",
        sender_id="A003",
        recipient_id="A002",
        minute=300,
    )
    capped = EventTriggeredSocialPolicy(
        recording_router, record_decision=lambda _: None, limits=limits
    ).consider_cooperation(
        before,
        initiator_agent_id="A003",
        participant_agent_id="A002",
        trigger_event_id="contact-event-4",
    )
    assert capped.reason == "agent_daily_cap"
    assert len(recording_router.provider.calls) == 1


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


def social_trigger_scenario() -> ScenarioSpec:
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
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


def social_trigger_router(
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
    world = WorldState.from_scenario(social_trigger_scenario())
    request_id = active_request(world)
    decisions = []
    router = social_trigger_router()
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
    world = WorldState.from_scenario(social_trigger_scenario())
    router = social_trigger_router()
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
    world = WorldState.from_scenario(social_trigger_scenario())
    request_id = active_request(world)
    decisions = []
    router = social_trigger_router("invented_action")
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
    world = WorldState.from_scenario(social_trigger_scenario())
    request_id = active_request(world)
    router = social_trigger_router()
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
    base = WorldState.from_scenario(social_trigger_scenario())
    request_id = active_request(base)
    pristine = WorldState.from_json(base.canonical_json())
    recorded_tape = ResponseTape()
    recording_router = social_trigger_router(tape=recorded_tape)
    EventTriggeredSocialPolicy(
        recording_router, record_decision=lambda _: None
    ).consider_support_request(base, agent_id="A001", request_id=request_id)
    changed = WorldState.from_json(pristine.canonical_json())
    changed.agents["A001"].trust = 0.21
    replay_router = social_trigger_router(mode=LLMMode.REPLAY, tape=recorded_tape)

    decision = EventTriggeredSocialPolicy(
        replay_router, record_decision=lambda _: None
    ).consider_support_request(changed, agent_id="A001", request_id=request_id)

    assert decision.accepted is False
    assert decision.reason == "rule_fallback:replay_miss"
    assert replay_router.api_request_count == 0
    assert replay_router.records[0].outcome == "replay_miss"


def test_checkpoint_replay_and_noop_continuations_are_canonically_identical() -> None:
    world = WorldState.from_scenario(social_trigger_scenario())
    request_id = active_request(world)
    checkpoint_before_decision = Checkpoint.capture(world)
    recorded_world = checkpoint_before_decision.restore_world()
    tape = ResponseTape()
    recorder = social_trigger_router(tape=tape)
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
    replay_routers = [
        social_trigger_router(mode=LLMMode.REPLAY),
        social_trigger_router(mode=LLMMode.REPLAY),
    ]
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
    selected_scenario = social_trigger_scenario()
    world = WorldState.from_scenario(selected_scenario)
    artifacts = RunArtifactWriter(selected_scenario, tmp_path / "run")
    artifacts.write_initial(world, execution_mode="record")
    request_id = active_request(world)
    router = social_trigger_router()
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
