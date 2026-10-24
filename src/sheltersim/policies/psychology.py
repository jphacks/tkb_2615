"""Deterministic offline updates for normalized Agent needs and stress."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.body_state import BODY_SOURCE_ID, canonical_body_config
from sheltersim.domain.models import Activity, NeedState, validate_unit_interval
from sheltersim.domain.psychological_state import update_psychological_state
from sheltersim.policies.behavior import WorldView


@dataclass(frozen=True)
class AgentStateDelta:
    """Absolute, validated state values computed from one immutable tick view."""

    agent_id: str
    minute_index: int
    toilet_urgency: float
    hunger: float
    fatigue: float
    stress: float
    trust: float
    hydration_need: float = 0.0
    sleep_debt_minutes: float | None = None
    uncertainty: float | None = None
    reassurance: float | None = None
    discontent: float | None = None
    positive_affect: float | None = None
    negative_affect: float | None = None
    hygiene_need: float | None = None

    def __post_init__(self) -> None:
        if not self.agent_id:
            raise ValueError("agent_id must be non-empty")
        if self.minute_index < 0:
            raise ValueError("minute_index cannot be negative")
        for name in (
            "toilet_urgency",
            "hunger",
            "fatigue",
            "stress",
            "trust",
            "hydration_need",
        ):
            validate_unit_interval(name, getattr(self, name))
        if self.sleep_debt_minutes is not None and (
            not isinstance(self.sleep_debt_minutes, (int, float))
            or isinstance(self.sleep_debt_minutes, bool)
            or not math.isfinite(self.sleep_debt_minutes)
            or not 0.0 <= self.sleep_debt_minutes <= 1440.0
        ):
            raise ValueError("sleep_debt_minutes must be within 0..1440 minutes")
        for name in (
            "uncertainty",
            "reassurance",
            "discontent",
            "positive_affect",
            "negative_affect",
            "hygiene_need",
        ):
            value = getattr(self, name)
            if value is not None:
                validate_unit_interval(name, value)


@dataclass(frozen=True)
class AgentPsychologyTrace:
    """Per-Agent decomposition of need changes and stress contribution sources."""

    agent_id: str
    needs_before: tuple[tuple[str, float], ...]
    needs_after: tuple[tuple[str, float], ...]
    stress_before: float
    stress_after: float
    highest_need: str | None
    highest_need_value: float
    need_pressure_threshold: float
    need_pressure: float
    need_pressure_stress: float
    queue_wait_stress: float
    support_wait_stress: float
    recovery_stress: float
    queue_task_id: str | None
    queue_kind: str | None
    queue_target_id: str | None
    queue_source_id: str | None
    queue_wait_minutes: int
    queue_facility_capacity: int | None
    queue_facility_working_units: int | None
    facility_event_context_ids: tuple[str, ...]
    support_request_id: str | None
    support_request_status: str | None
    support_assigned_staff_id: str | None
    support_wait_minutes: int
    environmental_stress: float
    environmental_stressor_event_ids: tuple[str, ...]
    environmental_assumption_source_id: str | None
    ambient_noise_level: float = 0.0
    rest_efficiency_multiplier: float = 1.0
    zone_environment_modifier_event_ids: tuple[str, ...] = ()
    zone_environment_assumption_ids: tuple[str, ...] = ()
    ventilation_effectiveness: float = 0.7
    lighting_level: float = 0.5
    privacy_level: float = 0.5
    temperature_c: float = 20.0
    condition_modifier_event_ids: tuple[str, ...] = ()
    condition_assumption_ids: tuple[str, ...] = ()
    life_state_feedback_stress: float = 0.0
    natural_recovery_stress: float = 0.0
    life_state_before: tuple[tuple[str, float], ...] = ()
    life_state_after: tuple[tuple[str, float], ...] = ()

    def to_dict(self, *, source_id: str) -> dict[str, Any]:
        """Build a detached JSON-compatible record with explicit cause references."""
        before = dict(self.needs_before)
        after = dict(self.needs_after)
        direct_causes: list[dict[str, Any]] = []
        if self.highest_need is not None:
            direct_causes.append(
                {
                    "kind": "unmet_need_pressure",
                    "need": self.highest_need,
                    "value": self.highest_need_value,
                    "threshold": self.need_pressure_threshold,
                    "excess_above_threshold": self.need_pressure,
                }
            )
        if self.queue_task_id is not None:
            direct_causes.append(
                {
                    "kind": "facility_queue_wait",
                    "queue_kind": self.queue_kind,
                    "queue_task_id": self.queue_task_id,
                    "source_id": self.queue_source_id,
                    "facility_id": self.queue_target_id,
                    "wait_minutes": self.queue_wait_minutes,
                    "facility_capacity": self.queue_facility_capacity,
                    "facility_working_units": self.queue_facility_working_units,
                    "capacity_event_context_ids": list(self.facility_event_context_ids),
                }
            )
        if self.support_request_id is not None:
            direct_causes.append(
                {
                    "kind": "staff_support_wait",
                    "support_request_id": self.support_request_id,
                    "status": self.support_request_status,
                    "assigned_staff_id": self.support_assigned_staff_id,
                    "wait_minutes": self.support_wait_minutes,
                }
            )
        if self.environmental_stress > 0.0:
            direct_causes.append(
                {
                    "kind": "experimental_environmental_stress",
                    "stress_per_minute": self.environmental_stress,
                    "stressor_event_ids": list(self.environmental_stressor_event_ids),
                    "assumption_source_id": self.environmental_assumption_source_id,
                }
            )
        if self.zone_environment_modifier_event_ids:
            direct_causes.append(
                {
                    "kind": "experimental_zone_noise_reduced_rest_efficiency",
                    "ambient_noise_level": self.ambient_noise_level,
                    "rest_efficiency_multiplier": self.rest_efficiency_multiplier,
                    "modifier_event_ids": list(self.zone_environment_modifier_event_ids),
                    "assumption_ids": list(self.zone_environment_assumption_ids),
                }
            )
        components = {
            "unmet_need_pressure": self.need_pressure_stress,
            "facility_queue_wait": self.queue_wait_stress,
            "staff_support_wait": self.support_wait_stress,
            "recovery": self.recovery_stress,
            "natural_recovery": self.natural_recovery_stress,
        }
        if self.life_state_feedback_stress:
            components["life_state_feedback"] = self.life_state_feedback_stress
        if self.environmental_stress > 0.0:
            components["experimental_environmental_stress"] = self.environmental_stress
        references = {
            "psychology_assumption_id": source_id,
            "queue_task_id": self.queue_task_id,
            "queue_source_id": self.queue_source_id,
            "facility_event_context_ids": list(self.facility_event_context_ids),
            "support_request_id": self.support_request_id,
            "support_source_id": (
                "ASM-STAFF-SUPPORT-LIFECYCLE-V1" if self.support_request_id is not None else None
            ),
        }
        if self.environmental_stress > 0.0:
            references["environmental_assumption_id"] = self.environmental_assumption_source_id
            references["environmental_stressor_event_ids"] = list(
                self.environmental_stressor_event_ids
            )
        document = {
            "agent_id": self.agent_id,
            "needs": {
                name: {
                    "before": before[name],
                    "after": after[name],
                    "delta": after[name] - before[name],
                }
                for name in before
            },
            "stress": {
                "before": self.stress_before,
                "after": self.stress_after,
                "applied_delta": self.stress_after - self.stress_before,
                "components": {
                    **components,
                },
                "raw_delta": (
                    self.need_pressure_stress
                    + self.queue_wait_stress
                    + self.support_wait_stress
                    + self.recovery_stress
                    + self.natural_recovery_stress
                    + self.life_state_feedback_stress
                ),
            },
            "direct_causes": direct_causes,
            "source_references": references,
        }
        if self.zone_environment_modifier_event_ids:
            document["zone_environment"] = {
                "ambient_noise_level": self.ambient_noise_level,
                "rest_efficiency_multiplier": self.rest_efficiency_multiplier,
                "modifier_event_ids": list(self.zone_environment_modifier_event_ids),
                "assumption_ids": list(self.zone_environment_assumption_ids),
                "source_type": "assumption",
            }
        if self.condition_modifier_event_ids or self.condition_assumption_ids:
            document["environment_conditions"] = {
                "ventilation_effectiveness": self.ventilation_effectiveness,
                "lighting_level": self.lighting_level,
                "privacy_level": self.privacy_level,
                "temperature_c": self.temperature_c,
                "modifier_event_ids": list(self.condition_modifier_event_ids),
                "assumption_ids": list(self.condition_assumption_ids),
                "source_type": "assumption",
            }
        if self.life_state_before:
            before_life = dict(self.life_state_before)
            after_life = dict(self.life_state_after)
            document["life_state"] = {
                name: {
                    "before": before_life[name],
                    "after": after_life[name],
                    "delta": after_life[name] - before_life[name],
                }
                for name in before_life
            }
        return document


@dataclass(frozen=True)
class PsychologyUpdateResult:
    """Pure psychology output plus a trace that does not feed back into state."""

    deltas: tuple[AgentStateDelta, ...]
    traces: tuple[AgentPsychologyTrace, ...]
    policy_version: str = "causal-stress-v1"

    def trace_document(
        self,
        minute: int,
        *,
        source_id: str,
        full: bool,
        maximum_agents: int = 20,
    ) -> dict[str, Any]:
        """Summarize every contribution while bounding summary-level detail."""
        if maximum_agents < 0:
            raise ValueError("maximum_agents cannot be negative")
        ordered = sorted(
            self.traces,
            key=lambda item: (
                -abs(item.stress_after - item.stress_before),
                item.agent_id,
            ),
        )
        sample = ordered if full else ordered[:maximum_agents]
        totals = {
            "unmet_need_pressure": math.fsum(item.need_pressure_stress for item in self.traces),
            "facility_queue_wait": math.fsum(item.queue_wait_stress for item in self.traces),
            "staff_support_wait": math.fsum(item.support_wait_stress for item in self.traces),
            "recovery": math.fsum(item.recovery_stress for item in self.traces),
            "natural_recovery": math.fsum(
                item.natural_recovery_stress for item in self.traces
            ),
            "applied_stress_delta": math.fsum(
                item.stress_after - item.stress_before for item in self.traces
            ),
        }
        environmental_total = math.fsum(item.environmental_stress for item in self.traces)
        if environmental_total:
            totals["experimental_environmental_stress"] = environmental_total
        feedback_total = math.fsum(item.life_state_feedback_stress for item in self.traces)
        if feedback_total:
            totals["life_state_feedback"] = feedback_total
        return {
            "minute_index": minute,
            "policy_version": self.policy_version,
            "assumption_id": source_id,
            "social_propagation_is_separate": True,
            "agent_count": len(self.traces),
            "truncated_agent_count": len(self.traces) - len(sample),
            "component_totals": totals,
            "agents": [item.to_dict(source_id=source_id) for item in sample],
        }


class PsychologyPolicy(Protocol):
    """Replaceable state updater that cannot directly mutate WorldState."""

    policy_version: str

    source_id: str

    def update(self, view: WorldView, minute: int) -> tuple[AgentStateDelta, ...]: ...

    def explain_update(self, view: WorldView, minute: int) -> PsychologyUpdateResult: ...


@dataclass(frozen=True)
class PsychologyParameters:
    """Explicit uncalibrated per-minute rates used by the baseline policy."""

    toilet_growth: float = 0.001
    hunger_growth: float = 0.0005
    awake_fatigue_growth: float = 0.0008
    resting_fatigue_recovery: float = 0.004
    sleeping_fatigue_recovery: float = 0.010
    stress_pressure: float = 0.012
    queued_stress: float = 0.002
    resting_stress_recovery: float = 0.003
    sleeping_stress_recovery: float = 0.006
    need_pressure_threshold: float = 0.35
    hydration_growth: float = 0.0
    support_wait_stress: float = 0.001
    toilet_growth_by_agent: dict[str, float] = field(default_factory=dict)
    hunger_growth_by_agent: dict[str, float] = field(default_factory=dict)
    awake_fatigue_growth_by_agent: dict[str, float] = field(default_factory=dict)
    natural_recovery_rate_per_minute: float = 0.01
    natural_recovery_rate_by_agent: dict[str, float] = field(default_factory=dict)
    baseline_distress_by_agent: dict[str, float] = field(default_factory=dict)
    chronic_retention: float = 0.90
    chronic_acute_weight: float = 0.08
    chronic_high_distress_weight: float = 0.02
    high_distress_threshold: float = 0.70

    def __post_init__(self) -> None:
        for name in (
            "toilet_growth",
            "hunger_growth",
            "awake_fatigue_growth",
            "resting_fatigue_recovery",
            "sleeping_fatigue_recovery",
            "stress_pressure",
            "queued_stress",
            "resting_stress_recovery",
            "sleeping_stress_recovery",
            "need_pressure_threshold",
            "hydration_growth",
            "support_wait_stress",
            "natural_recovery_rate_per_minute",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
            if name != "need_pressure_threshold" and value > 0.25:
                raise ValueError(f"{name} cannot exceed 0.25 per minute")
        if self.need_pressure_threshold > 1.0:
            raise ValueError("need_pressure_threshold cannot exceed one")
        for field_name in (
            "toilet_growth_by_agent",
            "hunger_growth_by_agent",
            "awake_fatigue_growth_by_agent",
        ):
            values = getattr(self, field_name)
            if len(values) > 10_000 or any(
                not isinstance(agent_id, str)
                or not agent_id
                or type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0.0 <= value <= 0.25
                for agent_id, value in values.items()
            ):
                raise ValueError(f"{field_name} must contain finite Agent rates in [0, 0.25]")
        for name in (
            "chronic_retention",
            "chronic_acute_weight",
            "chronic_high_distress_weight",
            "high_distress_threshold",
        ):
            validate_unit_interval(name, getattr(self, name))
        if len(self.baseline_distress_by_agent) > 10_000 or any(
            not isinstance(agent_id, str)
            or not agent_id
            or type(value) not in (int, float)
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
            for agent_id, value in self.baseline_distress_by_agent.items()
        ):
            raise ValueError("baseline_distress_by_agent must contain values in [0, 1]")
        if len(self.natural_recovery_rate_by_agent) > 10_000 or any(
            not isinstance(agent_id, str)
            or not agent_id
            or type(value) not in (int, float)
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not 0.0 <= value <= 0.25
            for agent_id, value in self.natural_recovery_rate_by_agent.items()
        ):
            raise ValueError("natural_recovery_rate_by_agent must contain rates in [0, 0.25]")


class SimplePsychologyPolicy:
    """Transparent, uncalibrated need and stress contributions for each Agent."""

    policy_version = "causal-stress-v1"

    def __init__(
        self,
        parameters: PsychologyParameters | None = None,
        *,
        source_id: str = "ASM-H04-CAUSAL-STRESS-V1",
    ) -> None:
        self.parameters = parameters or PsychologyParameters()
        if not source_id:
            raise ValueError("source_id must be non-empty")
        self.source_id = source_id

    def update(self, view: WorldView, minute: int) -> tuple[AgentStateDelta, ...]:
        """Return next state values without changing the view or world."""
        return self.explain_update(view, minute).deltas

    def explain_update(self, view: WorldView, minute: int) -> PsychologyUpdateResult:
        """Return state deltas and separate causal contributions for each Agent."""
        if minute != view.minute_index:
            raise ValueError("psychology update minute must match the WorldView")

        params = self.parameters
        deltas: list[AgentStateDelta] = []
        traces: list[AgentPsychologyTrace] = []
        for agent in view.agents.values():
            toilet_growth = params.toilet_growth_by_agent.get(
                agent.id, params.toilet_growth
            )
            hunger_growth = params.hunger_growth_by_agent.get(
                agent.id, params.hunger_growth
            )
            if agent.activity is Activity.SLEEPING:
                fatigue_delta = -params.sleeping_fatigue_recovery * agent.rest_efficiency_multiplier
                stress_recovery = params.sleeping_stress_recovery
            elif agent.activity is Activity.RESTING:
                fatigue_delta = -params.resting_fatigue_recovery * agent.rest_efficiency_multiplier
                stress_recovery = params.resting_stress_recovery
            else:
                fatigue_delta = params.awake_fatigue_growth_by_agent.get(
                    agent.id, params.awake_fatigue_growth
                )
                stress_recovery = 0.0

            needs = [
                ("toilet_urgency", agent.needs.toilet_urgency),
                ("hunger", agent.needs.hunger),
                ("fatigue", agent.needs.fatigue),
            ]
            if view.resource_service_enabled:
                needs.append(("hydration_need", agent.needs.hydration_need))
            highest_need, highest_need_value = max(needs, key=lambda item: item[1])
            unmet_need_pressure = max(0.0, highest_need_value - params.need_pressure_threshold)
            need_pressure_stress = (
                params.stress_pressure * unmet_need_pressure * agent.stress_sensitivity
            )
            queue_wait_stress = params.queued_stress if agent.activity is Activity.QUEUED else 0.0
            has_open_support_request = agent.support_request_status in ("pending", "assigned")
            support_wait_stress = params.support_wait_stress if has_open_support_request else 0.0
            environmental_stress = agent.environmental_stress_per_minute * agent.stress_sensitivity
            stress_delta = (
                need_pressure_stress
                + queue_wait_stress
                + support_wait_stress
                + environmental_stress
                - stress_recovery
            )
            new_values = {
                "toilet_urgency": _clamp(agent.needs.toilet_urgency + toilet_growth),
                "hunger": _clamp(agent.needs.hunger + hunger_growth),
                "fatigue": _clamp(agent.needs.fatigue + fatigue_delta),
                "hydration_need": (
                    _clamp(agent.needs.hydration_need + params.hydration_growth)
                    if view.resource_service_enabled
                    else 0.0
                ),
                "stress": _clamp(agent.stress + stress_delta),
            }
            delta = AgentStateDelta(
                agent_id=agent.id,
                minute_index=minute,
                toilet_urgency=new_values["toilet_urgency"],
                hunger=new_values["hunger"],
                fatigue=new_values["fatigue"],
                hydration_need=new_values["hydration_need"],
                stress=new_values["stress"],
                # Trust is unchanged until explicit support/service outcomes exist.
                trust=agent.trust,
            )
            deltas.append(delta)
            traces.append(
                AgentPsychologyTrace(
                    agent_id=agent.id,
                    needs_before=tuple(needs),
                    needs_after=tuple((name, new_values[name]) for name, _ in needs),
                    stress_before=agent.stress,
                    stress_after=delta.stress,
                    highest_need=highest_need,
                    highest_need_value=highest_need_value,
                    need_pressure_threshold=params.need_pressure_threshold,
                    need_pressure=unmet_need_pressure,
                    need_pressure_stress=need_pressure_stress,
                    queue_wait_stress=queue_wait_stress,
                    support_wait_stress=support_wait_stress,
                    recovery_stress=-stress_recovery,
                    queue_task_id=agent.queue_task_id,
                    queue_kind=agent.queue_kind,
                    queue_target_id=agent.queue_target_id,
                    queue_source_id=(
                        "ASM-TOILET-SERVICE-DURATION-V1"
                        if agent.queue_kind == "toilet_queue"
                        else (
                            "ASM-H02-RESOURCE-SERVICE-V1"
                            if agent.queue_kind == "resource_service_queue"
                            else None
                        )
                    ),
                    queue_wait_minutes=agent.queue_wait_minutes,
                    queue_facility_capacity=(
                        view.facilities[agent.queue_target_id].capacity
                        if agent.queue_target_id in view.facilities
                        else None
                    ),
                    queue_facility_working_units=(
                        view.facilities[agent.queue_target_id].working_units
                        if agent.queue_target_id in view.facilities
                        else None
                    ),
                    facility_event_context_ids=(
                        view.facilities[agent.queue_target_id].facility_event_context_ids
                        if agent.queue_target_id in view.facilities
                        else ()
                    ),
                    support_request_id=agent.support_request_id,
                    support_request_status=agent.support_request_status,
                    support_assigned_staff_id=agent.support_assigned_staff_id,
                    support_wait_minutes=agent.support_wait_minutes,
                    environmental_stress=environmental_stress,
                    environmental_stressor_event_ids=agent.environmental_stressor_event_ids,
                    environmental_assumption_source_id=agent.environmental_assumption_source_id,
                    ambient_noise_level=agent.ambient_noise_level,
                    rest_efficiency_multiplier=agent.rest_efficiency_multiplier,
                    zone_environment_modifier_event_ids=(agent.zone_environment_modifier_event_ids),
                    zone_environment_assumption_ids=agent.zone_environment_assumption_ids,
                    ventilation_effectiveness=agent.ventilation_effectiveness,
                    lighting_level=agent.lighting_level,
                    privacy_level=agent.privacy_level,
                    temperature_c=agent.temperature_c,
                    condition_modifier_event_ids=agent.condition_modifier_event_ids,
                    condition_assumption_ids=agent.condition_assumption_ids,
                )
            )
        return PsychologyUpdateResult(tuple(deltas), tuple(traces))


class MeanRevertingDistressPolicy(SimplePsychologyPolicy):
    """Continuous-time mean reversion plus a separately checkpointed daily latent state."""

    policy_version = "mean-reverting-distress-v1"
    source_id = "ASM-MEAN-REVERTING-ACUTE-DISTRESS-V1"

    def __init__(self, parameters: PsychologyParameters | None = None) -> None:
        super().__init__(parameters, source_id=self.source_id)

    def explain_update(self, view: WorldView, minute: int) -> PsychologyUpdateResult:
        baseline = super().explain_update(view, minute)
        deltas: list[AgentStateDelta] = []
        traces: list[AgentPsychologyTrace] = []
        for delta, trace in zip(baseline.deltas, baseline.traces, strict=True):
            kappa = self.parameters.natural_recovery_rate_by_agent.get(
                delta.agent_id, self.parameters.natural_recovery_rate_per_minute
            )
            personal_baseline = self.parameters.baseline_distress_by_agent.get(
                delta.agent_id, 0.0
            )
            natural_only = mean_reverting_step(
                trace.stress_before, personal_baseline, 0.0, kappa, 1.0
            )
            forcing = (
                trace.need_pressure_stress
                + trace.queue_wait_stress
                + trace.support_wait_stress
                + trace.environmental_stress
                + trace.recovery_stress
                + trace.life_state_feedback_stress
            )
            stress = mean_reverting_step(
                trace.stress_before, personal_baseline, forcing, kappa, 1.0
            )
            deltas.append(replace(delta, stress=stress))
            traces.append(
                replace(
                    trace,
                    stress_after=stress,
                    natural_recovery_stress=natural_only - trace.stress_before,
                )
            )
        return PsychologyUpdateResult(
            tuple(deltas), tuple(traces), policy_version=self.policy_version
        )

    def update_long_term_state(
        self,
        runtime_state: dict[str, Any],
        minute: int,
        final_deltas: tuple[AgentStateDelta, ...],
    ) -> dict[str, Any]:
        """Accumulate acute distress after social influence and update D once per day."""
        return update_psychological_state(
            runtime_state,
            minute=minute,
            acute_by_agent={delta.agent_id: delta.stress for delta in final_deltas},
            retention=self.parameters.chronic_retention,
            acute_weight=self.parameters.chronic_acute_weight,
            high_distress_weight=self.parameters.chronic_high_distress_weight,
            high_distress_threshold=self.parameters.high_distress_threshold,
            baseline_by_agent=self.parameters.baseline_distress_by_agent,
        )


class LifeStatePsychologyPolicy:
    """Registered, experimental extension for sleep debt and affect feedback."""

    policy_version = "life-state-feedback-v1"
    source_id = BODY_SOURCE_ID

    def __init__(
        self,
        parameters: PsychologyParameters,
        *,
        body_config: Any = None,
        body_formula: Any = None,
    ) -> None:
        self.base = SimplePsychologyPolicy(parameters, source_id=BODY_SOURCE_ID)
        self.body_config = canonical_body_config(body_config)
        self.body_formula = body_formula
        self.parameters = parameters

    def update(self, view: WorldView, minute: int) -> tuple[AgentStateDelta, ...]:
        return self.explain_update(view, minute).deltas

    def explain_update(self, view: WorldView, minute: int) -> PsychologyUpdateResult:
        """Return deterministic per-minute state deltas from the same observed view."""
        result = self.base.explain_update(view, minute)
        config = self.body_config
        values: list[AgentStateDelta] = []
        feedback_by_agent: dict[str, float] = {}
        stress_after_by_agent: dict[str, float] = {}
        life_before: dict[str, tuple[tuple[str, float], ...]] = {}
        life_after: dict[str, tuple[tuple[str, float], ...]] = {}
        for delta in result.deltas:
            agent = view.agents[delta.agent_id]
            household_peers = view.group_members.get(agent.group_id or "", ())
            family_separated = bool(
                household_peers
                and any(
                    peer_id != agent.id
                    and view.agents[peer_id].zone_id != agent.zone_id
                    for peer_id in household_peers
                )
            )
            family_separation_pressure = (
                config["discontent_stress_rate"] if family_separated else 0.0
            )
            debt = agent.sleep_debt_minutes
            awake_debt_rate = config["awake_sleep_debt_minutes_per_minute"]
            if agent.required_sleep_minutes is not None and agent.required_sleep_minutes > 0:
                awake_debt_rate = min(
                    10.0,
                    agent.required_sleep_minutes / max(1, 1440 - agent.required_sleep_minutes),
                )
            if agent.activity is Activity.SLEEPING:
                debt -= config["sleep_sleep_debt_recovery_per_minute"] * agent.rest_efficiency_multiplier
            elif agent.activity is Activity.RESTING:
                debt -= config["rest_sleep_debt_recovery_per_minute"] * agent.rest_efficiency_multiplier
            else:
                debt += awake_debt_rate
            debt = min(1440.0, max(0.0, debt))

            reassurance = max(0.0, agent.reassurance - config["reassurance_decay_per_minute"])
            uncertainty = min(
                1.0,
                max(
                    0.0,
                    agent.uncertainty
                    + agent.information_query_pressure * config["uncertainty_drift_per_minute"]
                    - config["uncertainty_decay_per_minute"]
                    - reassurance * config["uncertainty_decay_per_minute"],
                ),
            )
            queue_pressure = config["discontent_queue_rate"] if agent.activity is Activity.QUEUED else 0.0
            need_pressure = max(
                agent.needs.toilet_urgency,
                agent.needs.hunger,
                agent.needs.fatigue,
                agent.needs.hydration_need,
            )
            support_relief = config["discontent_relief_per_minute"] if agent.activity in (Activity.RESTING, Activity.SLEEPING) and agent.support_request_status not in ("pending", "assigned") else 0.0
            discontent = min(
                1.0,
                max(
                    0.0,
                    agent.discontent
                    + delta.stress * config["discontent_stress_rate"]
                    + queue_pressure
                    + agent.privacy_discontent_per_minute
                    + max(0.0, need_pressure - 0.5) * config["discontent_stress_rate"]
                    - support_relief
                    - reassurance * config["discontent_relief_per_minute"],
                ),
            )
            positive = 0.5 + (agent.positive_affect - 0.5) * max(
                0.0, 1.0 - config["positive_affect_decay_per_minute"]
            )
            hygiene_need = min(
                1.0,
                max(
                    0.0,
                    agent.hygiene_need
                    + config["hygiene_growth_per_minute"]
                    + agent.ventilation_hygiene_growth_per_minute
                    + (1.0 - agent.sanitation_level)
                    * config["sanitation_hygiene_growth_per_minute"],
                ),
            )
            negative = min(
                1.0,
                max(
                    0.0,
                    agent.negative_affect
                    + delta.stress * config["negative_affect_stress_rate"]
                    - reassurance * config["negative_affect_stress_rate"],
                ),
            )
            if self.body_formula is None:
                feedback_stress = (
                    debt / 1440.0 * config["sleep_debt_stress_rate"]
                    + uncertainty * config["negative_affect_stress_rate"]
                    + discontent * config["discontent_stress_rate"]
                    + hygiene_need * config["sanitation_stress_rate"]
                    + (1.0 - agent.sanitation_level) * config["sanitation_stress_rate"]
                    + family_separation_pressure
                    - reassurance * config["negative_affect_stress_rate"]
                    - positive * config["discontent_relief_per_minute"]
                )
            else:
                feedback_stress = self.body_formula.stress_feedback(
                    sleep_debt_minutes=debt,
                    uncertainty=uncertainty,
                    discontent=discontent,
                    hygiene_need=hygiene_need,
                    sanitation_level=agent.sanitation_level,
                    family_separation_pressure=family_separation_pressure,
                    reassurance=reassurance,
                    positive_affect=positive,
                    config=config,
                    agent_id=delta.agent_id,
                )
            feedback_by_agent[delta.agent_id] = feedback_stress
            updated = replace(
                delta,
                stress=min(1.0, max(0.0, delta.stress + feedback_stress)),
                sleep_debt_minutes=debt,
                uncertainty=uncertainty,
                reassurance=reassurance,
                discontent=discontent,
                positive_affect=positive,
                negative_affect=negative,
                hygiene_need=hygiene_need,
            )
            stress_after_by_agent[delta.agent_id] = updated.stress
            values.append(updated)
            life_before[delta.agent_id] = (
                ("sleep_debt_minutes", agent.sleep_debt_minutes),
                ("uncertainty", agent.uncertainty),
                ("reassurance", agent.reassurance),
                ("discontent", agent.discontent),
                ("positive_affect", agent.positive_affect),
                ("negative_affect", agent.negative_affect),
                ("hygiene_need", agent.hygiene_need),
                ("family_separation_pressure", family_separation_pressure),
            )
            life_after[delta.agent_id] = (
                ("sleep_debt_minutes", debt),
                ("uncertainty", uncertainty),
                ("reassurance", reassurance),
                ("discontent", discontent),
                ("positive_affect", positive),
                ("negative_affect", negative),
                ("hygiene_need", hygiene_need),
                ("family_separation_pressure", family_separation_pressure),
            )
        traces = tuple(
            replace(
                trace,
                stress_after=stress_after_by_agent.get(
                    trace.agent_id, trace.stress_after
                ),
                life_state_feedback_stress=feedback_by_agent.get(trace.agent_id, 0.0),
                life_state_before=life_before.get(trace.agent_id, ()),
                life_state_after=life_after.get(trace.agent_id, ()),
            )
            for trace in result.traces
        )
        return PsychologyUpdateResult(tuple(values), traces, self.policy_version)


def apply_state_deltas(world: WorldState, deltas: tuple[AgentStateDelta, ...]) -> None:
    """Apply a complete, current-tick update after validating every delta first."""
    seen: set[str] = set()
    prepared: list[tuple[str, AgentStateDelta]] = []
    for delta in deltas:
        if delta.agent_id in seen:
            raise StateInvariantError(f"duplicate psychology update for Agent {delta.agent_id!r}")
        if delta.agent_id not in world.agents:
            raise StateInvariantError(
                f"psychology update references unknown Agent {delta.agent_id!r}"
            )
        if delta.minute_index != world.minute_index:
            raise StateInvariantError("psychology update was computed for a different minute")
        # Revalidate in case an object was constructed through unsafe deserialization.
        try:
            NeedState(
                toilet_urgency=delta.toilet_urgency,
                hunger=delta.hunger,
                fatigue=delta.fatigue,
                hydration_need=delta.hydration_need,
            ).validate()
            validate_unit_interval("stress", delta.stress)
            validate_unit_interval("trust", delta.trust)
        except ValueError as exc:
            raise StateInvariantError(f"invalid psychology update: {exc}") from exc
        supplied_body = any(
            getattr(delta, name) is not None
            for name in (
                "sleep_debt_minutes",
                "uncertainty",
                "reassurance",
                "discontent",
                "positive_affect",
                "negative_affect",
                "hygiene_need",
            )
        )
        body_state = world.model_state.get("body_state_v1")
        if supplied_body and (
            not isinstance(body_state, dict)
            or delta.agent_id not in body_state.get("agents", {})
        ):
            raise StateInvariantError("body-state deltas require initialized body_state_v1")
        seen.add(delta.agent_id)
        prepared.append((delta.agent_id, delta))

    for agent_id, delta in prepared:
        agent = world.agents[agent_id]
        agent.needs.toilet_urgency = delta.toilet_urgency
        agent.needs.hunger = delta.hunger
        agent.needs.fatigue = delta.fatigue
        agent.needs.hydration_need = delta.hydration_need
        agent.stress = delta.stress
        agent.trust = delta.trust
        body_state = world.model_state.get("body_state_v1")
        body_values = {
            "sleep_debt_minutes": delta.sleep_debt_minutes,
            "uncertainty": delta.uncertainty,
            "reassurance": delta.reassurance,
            "discontent": delta.discontent,
            "positive_affect": delta.positive_affect,
            "negative_affect": delta.negative_affect,
            "hygiene_need": delta.hygiene_need,
        }
        supplied = {key: value for key, value in body_values.items() if value is not None}
        if supplied:
            body_state["agents"][delta.agent_id].update(supplied)


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def mean_reverting_step(
    value: float,
    baseline: float,
    forcing_per_minute: float,
    recovery_rate_per_minute: float,
    delta_minutes: float,
) -> float:
    """Exact one-step solution for constant forcing over a finite interval."""
    for name, item in (("value", value), ("baseline", baseline)):
        validate_unit_interval(name, item)
    if (
        not math.isfinite(forcing_per_minute)
        or not math.isfinite(recovery_rate_per_minute)
        or recovery_rate_per_minute < 0.0
        or not math.isfinite(delta_minutes)
        or delta_minutes <= 0.0
    ):
        raise ValueError("mean-reversion forcing, rate, or interval is invalid")
    decay = math.exp(-recovery_rate_per_minute * delta_minutes)
    if recovery_rate_per_minute == 0.0:
        result = value + forcing_per_minute * delta_minutes
    else:
        forcing_integral = -math.expm1(-recovery_rate_per_minute * delta_minutes)
        result = (
            baseline
            + (value - baseline) * decay
            + forcing_per_minute * forcing_integral / recovery_rate_per_minute
        )
    return _clamp(result)
