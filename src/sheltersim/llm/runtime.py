"""Selective, event-triggered Haiku proposals for bounded social interactions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from sheltersim.core.world import WorldState
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.models import (
    AgentInformationState,
    InformationBeliefStatus,
    InformationTruthStatus,
    SupportRequestStatus,
    TypedMessage,
)
from sheltersim.domain.social_dialogue import capture_social_state_context
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.llm.contracts import LLMStage, LLMTask
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.schemas import SOCIAL_ACTION_SCHEMA, social_interaction_proposal_schema

SOCIAL_MODEL_STATE_KEY = "social_runtime_v1"
SOCIAL_LIMITS_SOURCE_ID = "ASM-EVENT-TRIGGERED-SOCIAL-LIMITS-V1"
SOCIAL_INTERACTIONS_SOURCE_ID = "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1"


@dataclass(frozen=True)
class SocialDecision:
    """A validated runtime proposal and the controller's acceptance decision."""

    minute: int
    agent_id: str
    request_id: str | None
    action: str | None
    accepted: bool
    reason: str
    event_id: str | None
    input_hash: str | None
    state_hash: str | None
    model_id: str | None
    cache_hit: bool
    fallback_used: bool
    trigger: str | None = None
    interaction_type: str = "support_request"
    target_agent_id: str | None = None
    trigger_id: str | None = None
    reason_short: str | None = None
    effect_code: str | None = None
    estimated_cost_usd: float = 0.0
    api_request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "minute": self.minute,
            "agent_id": self.agent_id,
            "request_id": self.request_id,
            "trigger": self.trigger or "pending_support_request",
            "interaction_type": self.interaction_type,
            "target_agent_id": self.target_agent_id,
            "trigger_id": self.trigger_id,
            "action": self.action,
            "accepted": self.accepted,
            "status": "accepted" if self.accepted else "rejected",
            "reason": self.reason,
            "reason_short": self.reason_short,
            "effect_code": self.effect_code,
            "event_id": self.event_id,
            "input_hash": self.input_hash,
            "state_hash": self.state_hash,
            "model_id": self.model_id,
            "estimated_cost_usd": self.estimated_cost_usd,
            "api_request_count": self.api_request_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "source_type": "assumption",
            "source_id": (
                SOCIAL_LIMITS_SOURCE_ID
                if self.interaction_type == "support_request"
                else SOCIAL_INTERACTIONS_SOURCE_ID
            ),
            "cache_hit": self.cache_hit,
            "fallback_used": self.fallback_used,
        }


@dataclass(frozen=True)
class SocialRuntimeLimits:
    """Bound the number and frequency of model-triggered interactions."""

    stress_threshold: float = 0.65
    cooldown_minutes: int = 240
    per_agent_per_day: int = 3
    global_per_day: int = 4
    event_delay_minutes: int = 1
    complaint_wait_minutes: int = 30

    def __post_init__(self) -> None:
        if not 0.0 <= self.stress_threshold <= 1.0:
            raise ValueError("social stress threshold must be in 0..1")
        if (
            self.cooldown_minutes < 0
            or self.event_delay_minutes <= 0
            or self.complaint_wait_minutes <= 0
        ):
            raise ValueError("social cooldown must be non-negative and delays positive")
        if self.per_agent_per_day <= 0 or self.global_per_day <= 0:
            raise ValueError("social daily request caps must be positive")


class EventTriggeredSocialPolicy:
    """Ask Haiku only for selected high-stress support requests, never per tick/Agent."""

    policy_version = "event-triggered-social-v2"

    def __init__(
        self,
        router: LLMRouter,
        *,
        record_decision: Callable[[dict[str, Any]], None],
        limits: SocialRuntimeLimits | None = None,
    ) -> None:
        self.router = router
        self.record_decision = record_decision
        self.limits = limits or SocialRuntimeLimits()

    def consider_complaint_response(
        self,
        world: WorldState,
        *,
        agent_id: str,
        request_id: str,
    ) -> SocialDecision:
        """Select a bounded response to a high-stress request that is still waiting."""
        agent = world.agents.get(agent_id)
        request = world.support_requests.get(request_id)
        if agent is None or request is None or request.agent_id != agent_id:
            raise ValueError("complaint response requires a matching Agent support request")
        if request.status is not SupportRequestStatus.PENDING:
            return self._reject(
                world,
                agent_id,
                request_id,
                "request_not_pending",
                trigger="long_waiting_support_request",
                interaction_type="complaint_response",
            )
        wait_minutes = world.minute_index - request.requested_minute
        if wait_minutes < self.limits.complaint_wait_minutes:
            return self._reject(
                world,
                agent_id,
                request_id,
                "complaint_wait_threshold_not_met",
                trigger="long_waiting_support_request",
                interaction_type="complaint_response",
            )
        if agent.stress < self.limits.stress_threshold:
            return self._reject(
                world,
                agent_id,
                request_id,
                "below_stress_threshold",
                trigger="long_waiting_support_request",
                interaction_type="complaint_response",
            )
        payload = {
            "interaction_type": "complaint_response",
            "agent_id": agent_id,
            "request_id": request_id,
            "zone_id": agent.zone_id,
            "stress_threshold": self.limits.stress_threshold,
            "complaint_wait_minutes": self.limits.complaint_wait_minutes,
        }
        return self._consider_typed_interaction(
            world,
            trigger="long_waiting_support_request",
            interaction_type="complaint_response",
            initiator_agent_id=agent_id,
            participant_agent_ids=(agent_id,),
            trigger_id=request_id,
            request_id=request_id,
            zone_id=agent.zone_id,
            relationship="pending_staff_support_request",
            observed_facts={
                "request_status": request.status.value,
                "wait_minutes": wait_minutes,
                "stress": agent.stress,
                "trust": agent.trust,
            },
            allowed_actions=(
                "acknowledge_complaint",
                "explain_staff_process",
                "share_practical_information",
                "no_interaction",
            ),
            reason_codes=("long_wait", "high_stress", "request_still_pending"),
            event_payload=payload,
        )

    def consider_cooperation(
        self,
        world: WorldState,
        *,
        initiator_agent_id: str,
        participant_agent_id: str,
        trigger_event_id: str,
    ) -> SocialDecision:
        """Consider a same-zone, relationship-checked offer of mutual support."""
        initiator = world.agents.get(initiator_agent_id)
        participant = world.agents.get(participant_agent_id)
        if initiator is None or participant is None or initiator_agent_id == participant_agent_id:
            raise ValueError("cooperation requires two different known Agent IDs")
        if initiator.zone_id != participant.zone_id:
            return self._reject(
                world,
                initiator_agent_id,
                None,
                "participants_not_in_same_zone",
                trigger="modeled_social_contact",
                interaction_type="cooperation",
                target_agent_id=participant_agent_id,
                trigger_id=trigger_event_id,
            )
        same_group = (
            initiator.profile.group_id is not None
            and initiator.profile.group_id == participant.profile.group_id
        )
        if not same_group:
            return self._reject(
                world,
                initiator_agent_id,
                None,
                "modeled_relationship_not_eligible",
                trigger="modeled_social_contact",
                interaction_type="cooperation",
                target_agent_id=participant_agent_id,
                trigger_id=trigger_event_id,
            )
        if max(initiator.stress, participant.stress) < self.limits.stress_threshold:
            return self._reject(
                world,
                initiator_agent_id,
                None,
                "participants_below_stress_threshold",
                trigger="modeled_social_contact",
                interaction_type="cooperation",
                target_agent_id=participant_agent_id,
                trigger_id=trigger_event_id,
            )
        payload = {
            "interaction_type": "cooperation",
            "agent_id": initiator_agent_id,
            "participant_agent_id": participant_agent_id,
            "zone_id": initiator.zone_id,
            "stress_threshold": self.limits.stress_threshold,
            "relationship": "same_group",
            "trigger_event_id": trigger_event_id,
        }
        return self._consider_typed_interaction(
            world,
            trigger="modeled_social_contact",
            interaction_type="cooperation",
            initiator_agent_id=initiator_agent_id,
            participant_agent_ids=(initiator_agent_id, participant_agent_id),
            trigger_id=trigger_event_id,
            request_id=None,
            target_agent_id=participant_agent_id,
            zone_id=initiator.zone_id,
            relationship="same_group",
            observed_facts={
                "initiator_stress": initiator.stress,
                "participant_stress": participant.stress,
                "contact_event_applied": True,
            },
            allowed_actions=("offer_mutual_support", "no_interaction"),
            reason_codes=("shared_group_contact", "elevated_stress"),
            event_payload=payload,
        )

    def consider_information_reaction(
        self,
        world: WorldState,
        *,
        agent_id: str,
        participant_agent_id: str,
        message_id: str,
        trigger_event_id: str,
    ) -> SocialDecision:
        """Offer a verification request after a locally heard false typed claim."""
        agent = world.agents.get(agent_id)
        participant = world.agents.get(participant_agent_id)
        state = world.model_state.get(INFORMATION_SPREAD_STATE_KEY)
        if agent is None or participant is None or agent_id == participant_agent_id:
            raise ValueError("information reaction requires two different known Agent IDs")
        if not isinstance(state, dict):
            return self._reject(
                world,
                agent_id,
                None,
                "information_state_unavailable",
                trigger="unverified_claim_contact",
                interaction_type="information_reaction",
                target_agent_id=participant_agent_id,
                trigger_id=trigger_event_id,
            )
        raw_message = state["messages"].get(message_id)
        raw_belief = state["beliefs"].get(agent_id, {}).get(message_id)
        if raw_message is None or raw_belief is None:
            return self._reject(
                world,
                agent_id,
                None,
                "information_claim_not_current",
                trigger="unverified_claim_contact",
                interaction_type="information_reaction",
                target_agent_id=participant_agent_id,
                trigger_id=trigger_event_id,
            )
        message = TypedMessage.from_dict(raw_message)
        belief = AgentInformationState.from_dict(raw_belief)
        if (
            message.truth_status is not InformationTruthStatus.FALSE
            or belief.belief is not InformationBeliefStatus.HEARD
        ):
            return self._reject(
                world,
                agent_id,
                None,
                "not_an_unverified_false_claim",
                trigger="unverified_claim_contact",
                interaction_type="information_reaction",
                target_agent_id=participant_agent_id,
                trigger_id=trigger_event_id,
            )
        if agent.zone_id != participant.zone_id:
            return self._reject(
                world,
                agent_id,
                None,
                "participants_not_in_same_zone",
                trigger="unverified_claim_contact",
                interaction_type="information_reaction",
                target_agent_id=participant_agent_id,
                trigger_id=trigger_event_id,
            )
        payload = {
            "interaction_type": "information_reaction",
            "agent_id": agent_id,
            "participant_agent_id": participant_agent_id,
            "zone_id": agent.zone_id,
            "message_id": message_id,
            "trigger_event_id": trigger_event_id,
        }
        return self._consider_typed_interaction(
            world,
            trigger="unverified_claim_contact",
            interaction_type="information_reaction",
            initiator_agent_id=agent_id,
            participant_agent_ids=(agent_id, participant_agent_id),
            trigger_id=trigger_event_id,
            request_id=None,
            target_agent_id=participant_agent_id,
            zone_id=agent.zone_id,
            relationship="same_zone_contact",
            observed_facts={
                "belief_status": "heard",
                "claim_is_locally_unverified": True,
            },
            allowed_actions=("seek_claim_verification", "no_interaction"),
            reason_codes=("heard_through_contact", "request_verification"),
            event_payload=payload,
        )

    def consider_support_request(
        self,
        world: WorldState,
        *,
        agent_id: str,
        request_id: str,
    ) -> SocialDecision:
        """Evaluate one support-request trigger in the canonical controller order."""
        agent = world.agents.get(agent_id)
        request = world.support_requests.get(request_id)
        if agent is None or request is None or request.agent_id != agent_id:
            raise ValueError("social proposal requires a matching Agent support request")
        minute = world.minute_index
        if request.status is not SupportRequestStatus.PENDING:
            return self._reject(world, agent_id, request_id, "request_not_pending")
        if agent.stress < self.limits.stress_threshold:
            return self._reject(world, agent_id, request_id, "below_stress_threshold")
        if self.router.mode.value == "offline":
            return self._reject(world, agent_id, request_id, "offline_rule_mode")
        due_minute = minute + self.limits.event_delay_minutes
        if due_minute >= world.duration_minutes:
            return self._reject(world, agent_id, request_id, "scenario_deadline")

        runtime = _social_runtime(world)
        day = minute // 1440
        day_key = str(day)
        agent_day_key = f"{day}:{agent_id}"
        attempts_by_day = runtime["attempts_by_day"]
        attempts_by_agent = runtime["attempts_by_agent_day"]
        last_attempt = runtime["last_attempt_minute"]
        if attempts_by_agent.get(agent_day_key, 0) >= self.limits.per_agent_per_day:
            return self._reject(world, agent_id, request_id, "agent_daily_cap")
        if attempts_by_day.get(day_key, 0) >= self.limits.global_per_day:
            return self._reject(world, agent_id, request_id, "global_daily_cap")
        prior_attempt = last_attempt.get(agent_id)
        if isinstance(prior_attempt, int) and minute - prior_attempt < self.limits.cooldown_minutes:
            return self._reject(world, agent_id, request_id, "cooldown")

        input_payload = {
            "trigger": "pending_support_request",
            "allowed_actions": list(SOCIAL_ACTION_SCHEMA["properties"]["action"]["enum"]),
            "constraints": [
                "Use only the anonymous functional context below.",
                "Do not infer demographics or create factual claims about people.",
                "Choose no_interaction when context does not support an action.",
                "Return exactly the required JSON object.",
            ],
            "minute": minute,
            "agent_context": {
                "stress": agent.stress,
                "trust": agent.trust,
                "support_need": agent.profile.support_need,
                "toilet_urgency": agent.needs.toilet_urgency,
                "hunger": agent.needs.hunger,
                "fatigue": agent.needs.fatigue,
            },
            "request_context": {
                "status": request.status.value,
                "wait_minutes": minute - request.requested_minute,
            },
        }
        input_text = json.dumps(
            input_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        state_hash = hashlib.sha256(world.canonical_json().encode("utf-8")).hexdigest()
        fallback = '{"action":"no_interaction"}'
        task = LLMTask(
            stage=LLMStage.RUNTIME,
            input_text=input_text,
            state_hash=state_hash,
            prompt_version=self.policy_version,
            schema_version=1,
            output_schema=SOCIAL_ACTION_SCHEMA,
            fallback_text=fallback,
            max_output_tokens=24,
        )
        response = self.router.complete(task)
        record = self.router.records[-1]
        runtime["attempts_by_day"][day_key] = attempts_by_day.get(day_key, 0) + 1
        runtime["attempts_by_agent_day"][agent_day_key] = (
            attempts_by_agent.get(agent_day_key, 0) + 1
        )
        runtime["last_attempt_minute"][agent_id] = minute
        proposal = json.loads(response)
        action = proposal["action"]
        if record.fallback_used:
            decision = SocialDecision(
                minute,
                agent_id,
                request_id,
                action,
                False,
                f"rule_fallback:{record.outcome}",
                None,
                record.input_hash,
                state_hash,
                record.model_id,
                record.cache_hit,
                True,
                trigger="pending_support_request",
                interaction_type="support_request",
                effect_code=_social_effect_code("support_request", action),
                estimated_cost_usd=record.estimated_cost_usd,
                api_request_count=record.api_request_count,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
            )
            self.record_decision(decision.to_dict())
            runtime["decision_count"] += 1
            return decision

        event_id: str | None = None
        if action == "no_interaction":
            reason = "model_selected_no_interaction"
        else:
            sequence = world.allocate_event_sequence()
            event_id = f"social-interaction-{sequence}"
            event = ScheduledEvent(
                event_id=event_id,
                kind=EventKind.ENDOGENOUS,
                due_minute=due_minute,
                priority=10,
                sequence=sequence,
                event_type="social_interaction",
                payload={
                    "agent_id": agent_id,
                    "request_id": request_id,
                    "action": action,
                },
                source_id=self.policy_version,
                causation_id=request_id,
                provenance={
                    "source_type": "assumption",
                    "source_id": SOCIAL_LIMITS_SOURCE_ID,
                    "policy_version": self.policy_version,
                    "model_id": record.model_id,
                    "prompt_version": self.policy_version,
                    "input_hash": record.input_hash,
                },
            )
            world.schedule_event(event)
            reason = "accepted_and_scheduled"
        decision = SocialDecision(
            minute,
            agent_id,
            request_id,
            action,
            True,
            reason,
            event_id,
            record.input_hash,
            state_hash,
            record.model_id,
            record.cache_hit,
            False,
            trigger="pending_support_request",
            interaction_type="support_request",
            effect_code=_social_effect_code("support_request", action),
            estimated_cost_usd=record.estimated_cost_usd,
            api_request_count=record.api_request_count,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
        )
        self.record_decision(decision.to_dict())
        runtime["decision_count"] += 1
        return decision

    def _reject(
        self,
        world: WorldState,
        agent_id: str,
        request_id: str | None,
        reason: str,
        *,
        trigger: str | None = None,
        interaction_type: str = "support_request",
        target_agent_id: str | None = None,
        trigger_id: str | None = None,
    ) -> SocialDecision:
        decision = SocialDecision(
            world.minute_index,
            agent_id,
            request_id,
            None,
            False,
            reason,
            None,
            None,
            None,
            None,
            False,
            False,
            trigger=trigger,
            interaction_type=interaction_type,
            target_agent_id=target_agent_id,
            trigger_id=trigger_id,
        )
        self.record_decision(decision.to_dict())
        return decision

    def _consider_typed_interaction(
        self,
        world: WorldState,
        *,
        trigger: str,
        interaction_type: str,
        initiator_agent_id: str,
        participant_agent_ids: tuple[str, ...],
        trigger_id: str,
        request_id: str | None,
        target_agent_id: str | None = None,
        zone_id: str,
        relationship: str,
        observed_facts: dict[str, Any],
        allowed_actions: tuple[str, ...],
        reason_codes: tuple[str, ...],
        event_payload: dict[str, Any],
    ) -> SocialDecision:
        minute = world.minute_index
        due_minute = minute + self.limits.event_delay_minutes
        if self.router.mode.value == "offline":
            return self._reject(
                world,
                initiator_agent_id,
                request_id,
                "offline_rule_mode",
                trigger=trigger,
                interaction_type=interaction_type,
                target_agent_id=target_agent_id,
                trigger_id=trigger_id,
            )
        if due_minute >= world.duration_minutes:
            return self._reject(
                world,
                initiator_agent_id,
                request_id,
                "scenario_deadline",
                trigger=trigger,
                interaction_type=interaction_type,
                target_agent_id=target_agent_id,
                trigger_id=trigger_id,
            )
        participants = tuple(sorted(set(participant_agent_ids)))
        if not participants or any(agent_id not in world.agents for agent_id in participants):
            return self._reject(
                world,
                initiator_agent_id,
                request_id,
                "participant_state_changed",
                trigger=trigger,
                interaction_type=interaction_type,
                target_agent_id=target_agent_id,
                trigger_id=trigger_id,
            )
        event_payload = {
            **event_payload,
            "conversation_state_context": capture_social_state_context(world, participants),
        }
        runtime = _social_runtime(world)
        day_key = str(minute // 1440)
        attempts_by_day = runtime["attempts_by_day"]
        attempts_by_agent = runtime["attempts_by_agent_day"]
        last_attempt = runtime["last_attempt_minute"]
        if attempts_by_day.get(day_key, 0) >= self.limits.global_per_day:
            return self._reject(
                world,
                initiator_agent_id,
                request_id,
                "global_daily_cap",
                trigger=trigger,
                interaction_type=interaction_type,
                target_agent_id=target_agent_id,
                trigger_id=trigger_id,
            )
        for agent_id in participants:
            agent_day_key = f"{day_key}:{agent_id}"
            if attempts_by_agent.get(agent_day_key, 0) >= self.limits.per_agent_per_day:
                return self._reject(
                    world,
                    initiator_agent_id,
                    request_id,
                    "agent_daily_cap",
                    trigger=trigger,
                    interaction_type=interaction_type,
                    target_agent_id=target_agent_id,
                    trigger_id=trigger_id,
                )
            prior_attempt = last_attempt.get(agent_id)
            if (
                isinstance(prior_attempt, int)
                and minute - prior_attempt < self.limits.cooldown_minutes
            ):
                return self._reject(
                    world,
                    initiator_agent_id,
                    request_id,
                    "cooldown",
                    trigger=trigger,
                    interaction_type=interaction_type,
                    target_agent_id=target_agent_id,
                    trigger_id=trigger_id,
                )

        candidate_identity = {
            "interaction_type": interaction_type,
            "minute": minute,
            "trigger_id": trigger_id,
            "agent_ids": participants,
            "zone_id": zone_id,
            "relationship": relationship,
            "event_context": event_payload,
        }
        proposal_id = (
            "social-interaction-"
            + hashlib.sha256(
                json.dumps(candidate_identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:16]
        )
        schema = social_interaction_proposal_schema(
            event_id=proposal_id,
            allowed_actions=list(allowed_actions),
            reason_codes=[*reason_codes, "rule_fallback"],
        )
        input_payload = {
            "event_id": proposal_id,
            "interaction_type": interaction_type,
            "minute": minute,
            "due_minute": due_minute,
            "agent_ids": list(participants),
            "zone_id": zone_id,
            "relationship": relationship,
            "observed_facts": observed_facts,
            "allowed_choices": list(allowed_actions),
            "reason_codes": list(reason_codes),
            "constraints": [
                "Select only a listed action and reason code.",
                "Use no conversation text and do not add facts.",
                "Choose no_interaction when context does not support an action.",
                "Echo the supplied event_id exactly.",
            ],
        }
        input_text = json.dumps(
            input_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        state_hash = hashlib.sha256(world.canonical_json().encode("utf-8")).hexdigest()
        fallback_text = json.dumps(
            {"event_id": proposal_id, "action": "no_interaction", "reason_short": "rule_fallback"},
            separators=(",", ":"),
        )
        task = LLMTask(
            stage=LLMStage.RUNTIME,
            input_text=input_text,
            state_hash=state_hash,
            prompt_version=f"{self.policy_version}-{interaction_type}",
            schema_version=1,
            output_schema=schema,
            fallback_text=fallback_text,
            # Runtime proposals include a long candidate ID and strict reason code;
            # 40 tokens can be consumed before the JSON object is complete.
            max_output_tokens=128,
        )
        response = self.router.complete(task)
        record = self.router.records[-1]
        attempts_by_day[day_key] = attempts_by_day.get(day_key, 0) + 1
        for agent_id in participants:
            agent_day_key = f"{day_key}:{agent_id}"
            attempts_by_agent[agent_day_key] = attempts_by_agent.get(agent_day_key, 0) + 1
            last_attempt[agent_id] = minute
        proposal = json.loads(response)
        action = proposal["action"]
        reason_short = proposal["reason_short"]
        if proposal["event_id"] != proposal_id:
            raise ValueError("social proposal event_id does not match its candidate")
        if record.fallback_used:
            decision = self._typed_decision(
                minute,
                initiator_agent_id,
                request_id,
                trigger,
                interaction_type,
                target_agent_id,
                trigger_id,
                action,
                False,
                f"rule_fallback:{record.outcome}",
                None,
                reason_short,
                record,
                state_hash,
            )
            runtime["decision_count"] += 1
            self.record_decision(decision.to_dict())
            return decision

        event_id: str | None = None
        if action == "no_interaction":
            reason = "model_selected_no_interaction"
        else:
            event_id = proposal_id
            sequence = world.allocate_event_sequence()
            payload = {
                **event_payload,
                "action": action,
                "reason_short": reason_short,
            }
            world.schedule_event(
                ScheduledEvent(
                    event_id=event_id,
                    kind=EventKind.ENDOGENOUS,
                    due_minute=due_minute,
                    priority=10,
                    sequence=sequence,
                    event_type="social_interaction",
                    payload=payload,
                    source_id=self.policy_version,
                    causation_id=trigger_id,
                    provenance={
                        "source_type": "assumption",
                        "source_id": SOCIAL_INTERACTIONS_SOURCE_ID,
                        "policy_version": self.policy_version,
                        "model_id": record.model_id,
                        "prompt_version": task.prompt_version,
                        "input_hash": record.input_hash,
                    },
                )
            )
            reason = "accepted_and_scheduled"
        decision = self._typed_decision(
            minute,
            initiator_agent_id,
            request_id,
            trigger,
            interaction_type,
            target_agent_id,
            trigger_id,
            action,
            True,
            reason,
            event_id,
            reason_short,
            record,
            state_hash,
        )
        runtime["decision_count"] += 1
        self.record_decision(decision.to_dict())
        return decision

    @staticmethod
    def _typed_decision(
        minute: int,
        agent_id: str,
        request_id: str | None,
        trigger: str,
        interaction_type: str,
        target_agent_id: str | None,
        trigger_id: str,
        action: str,
        accepted: bool,
        reason: str,
        event_id: str | None,
        reason_short: str,
        record: Any,
        state_hash: str,
    ) -> SocialDecision:
        return SocialDecision(
            minute=minute,
            agent_id=agent_id,
            request_id=request_id,
            action=action,
            accepted=accepted,
            reason=reason,
            event_id=event_id,
            input_hash=record.input_hash,
            state_hash=state_hash,
            model_id=record.model_id,
            cache_hit=record.cache_hit,
            fallback_used=record.fallback_used,
            trigger=trigger,
            interaction_type=interaction_type,
            target_agent_id=target_agent_id,
            trigger_id=trigger_id,
            reason_short=reason_short,
            effect_code=_social_effect_code(interaction_type, action),
            estimated_cost_usd=record.estimated_cost_usd,
            api_request_count=record.api_request_count,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
        )


def _social_runtime(world: WorldState) -> dict[str, Any]:
    runtime = world.model_state.setdefault(
        SOCIAL_MODEL_STATE_KEY,
        {
            "schema_version": 1,
            "attempts_by_day": {},
            "attempts_by_agent_day": {},
            "last_attempt_minute": {},
            "decision_count": 0,
        },
    )
    expected = {
        "schema_version",
        "attempts_by_day",
        "attempts_by_agent_day",
        "last_attempt_minute",
        "decision_count",
    }
    if not isinstance(runtime, dict) or set(runtime) != expected or runtime["schema_version"] != 1:
        raise ValueError("saved social runtime state is incompatible")
    for key in ("attempts_by_day", "attempts_by_agent_day", "last_attempt_minute"):
        if not isinstance(runtime[key], dict):
            raise ValueError(f"saved social runtime {key} must be an object")
    if not isinstance(runtime["decision_count"], int) or isinstance(
        runtime["decision_count"], bool
    ):
        raise ValueError("saved social runtime decision_count must be an integer")
    return runtime


def _social_effect_code(interaction_type: str, action: str | None) -> str | None:
    """Return a fixed audit label for the modeled effect of one selected action."""
    return {
        ("support_request", "offer_reassurance"): "requester_stress_relief_and_trust_increase",
        ("support_request", "share_practical_information"): "requester_trust_increase",
        (
            "complaint_response",
            "acknowledge_complaint",
        ): "requester_stress_relief_and_trust_increase",
        (
            "complaint_response",
            "explain_staff_process",
        ): "requester_stress_relief_and_trust_increase",
        ("complaint_response", "share_practical_information"): "requester_trust_increase",
        ("cooperation", "offer_mutual_support"): "both_agents_stress_relief_and_trust_increase",
        (
            "information_reaction",
            "seek_claim_verification",
        ): "verification_query_pressure_increased",
    }.get((interaction_type, action))
