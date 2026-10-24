"""Versioned structured output schemas used at the LLM boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)

from sheltersim.domain.environment import ScenarioZoneEnvironmentConfig
from sheltersim.domain.models import Mobility
from sheltersim.events.effects import (
    CompilerProvenance,
    EffectProgram,
    GuardExpression,
    NonNegativeMinute,
    PlanAssumption,
    StableId,
    StrictContract,
    SupportMode,
    TargetSelector,
    TimedAction,
)
from sheltersim.experiments.scenario import ScenarioSpec

__all__ = [
    "Ambiguity",
    "ApprovedPlan",
    "CompilerResult",
    "CompilerUsage",
    "CompilerProvenance",
    "DraftPlan",
    "MultiPlanAlternative",
    "MultiPlanApproval",
    "MultiPlanDraft",
    "multi_plan_effect_signature",
    "EffectProgram",
    "FacilityTarget",
    "GuardExpression",
    "NonNegativeMinute",
    "ParseIssue",
    "ParseRequest",
    "ParseResult",
    "PlanAssumption",
    "ScenarioSummary",
    "ShipmentTarget",
    "StableId",
    "StrictContract",
    "SupportMode",
    "TargetSelector",
    "TimedAction",
    "ZoneTarget",
]


class FacilityTarget(StrictContract):
    id: StableId
    name: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]
    kind: StableId
    zone_id: StableId | None = None
    capacity: Annotated[int, Field(strict=True, ge=0, le=1000)]
    working_units: Annotated[int, Field(strict=True, ge=0, le=1000)]
    active_occupancy: Annotated[int, Field(strict=True, ge=0, le=1000)] = 0

    @model_validator(mode="after")
    def working_capacity_is_valid(self) -> FacilityTarget:
        if self.working_units > self.capacity:
            raise ValueError("working_units cannot exceed facility capacity")
        if self.active_occupancy > self.working_units:
            raise ValueError("active_occupancy cannot exceed working_units")
        return self


class ZoneTarget(StrictContract):
    id: StableId
    name: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]
    has_water_station: bool = False


class ShipmentTarget(StrictContract):
    id: StableId
    resource_id: StableId
    due_minute: NonNegativeMinute


class ScenarioSummary(StrictContract):
    """Stable, anonymous scenario facts available to parsers and the Opus compiler."""

    duration_minutes: Annotated[int, Field(strict=True, gt=0, le=60 * 24 * 366)] | None = None
    start_date: date | None = None
    default_zone_id: StableId | None = None
    agent_count: Annotated[int, Field(strict=True, ge=0, le=10_000)] = 0
    facilities: list[FacilityTarget] = Field(default_factory=list, max_length=200)
    zones: list[ZoneTarget] = Field(default_factory=list, max_length=100)
    staff_ids: list[StableId] = Field(default_factory=list, max_length=100)
    available_staff_ids: list[StableId] = Field(default_factory=list, max_length=100)
    resource_ids: list[StableId] = Field(default_factory=list, max_length=10)
    warehouse_id: StableId | None = None
    pending_shipments: list[ShipmentTarget] = Field(default_factory=list, max_length=500)
    available_policies: list[StableId] = Field(default_factory=list, max_length=50)
    person_ids: list[StableId] = Field(default_factory=list, max_length=10000)
    route_ids: list[StableId] = Field(default_factory=list, max_length=2000)
    person_targets_truncated: bool = False
    zone_environment_settings: ScenarioZoneEnvironmentConfig | None = None

    @model_validator(mode="after")
    def summary_ids_are_unique(self) -> ScenarioSummary:
        if len({item.id for item in self.facilities}) != len(self.facilities):
            raise ValueError("facility IDs in scenario summary must be unique")
        if len({item.id for item in self.zones}) != len(self.zones):
            raise ValueError("zone IDs in scenario summary must be unique")
        for label, values in (
            ("staff", self.staff_ids),
            ("available staff", self.available_staff_ids),
            ("resource", self.resource_ids),
            ("shipment", [item.id for item in self.pending_shipments]),
            ("policy", self.available_policies),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{label} IDs in scenario summary must be unique")
        if not set(self.available_staff_ids) <= set(self.staff_ids):
            raise ValueError("available staff IDs must be listed in staff_ids")
        if self.resource_ids and self.warehouse_id is None:
            raise ValueError("resource targets require a warehouse_id")
        if self.duration_minutes is not None and any(
            item.due_minute >= self.duration_minutes for item in self.pending_shipments
        ):
            raise ValueError("pending shipment minutes must be within the scenario")
        if self.default_zone_id is not None and self.default_zone_id not in {
            item.id for item in self.zones
        }:
            raise ValueError("default_zone_id must reference a listed zone")
        zone_ids = {item.id for item in self.zones}
        if any(
            item.zone_id is not None and item.zone_id not in zone_ids for item in self.facilities
        ):
            raise ValueError("facility zone_id must reference a listed zone")
        return self


class ParseRequest(StrictContract):
    text: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000)]
    branch_id: StableId
    selected_minute: NonNegativeMinute
    scenario_summary: ScenarioSummary = Field(default_factory=ScenarioSummary)

    @model_validator(mode="after")
    def selected_minute_is_in_scenario(self) -> ParseRequest:
        duration = self.scenario_summary.duration_minutes
        if duration is not None and self.selected_minute >= duration:
            raise ValueError("selected_minute must be before the scenario end")
        return self


class Ambiguity(StrictContract):
    code: StableId
    field: StableId
    message: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    choices: list[StableId] = Field(default_factory=list, max_length=100)


class DraftPlan(EffectProgram):
    """Previewable plan consumed by offline parsing, later Opus compilation, and validation."""

    branch_id: StableId
    fork_at_minute: NonNegativeMinute
    ambiguities: list[Ambiguity] = Field(default_factory=list, max_length=50)
    requires_confirmation: bool = True

    @model_validator(mode="after")
    def plan_requires_explicit_approval(self) -> DraftPlan:
        if not self.requires_confirmation:
            raise ValueError("DraftPlan requires explicit user confirmation")
        if self.simulation_start_minute != self.fork_at_minute:
            raise ValueError("simulation_start_minute must match fork_at_minute")
        if self.ambiguities and self.support_mode is not SupportMode.CLARIFICATION_REQUIRED:
            raise ValueError("ambiguous plans must use clarification_required support mode")
        return self


class ApprovedPlan(StrictContract):
    """A preview plan tied to an explicit confirmation of its exact contents."""

    draft_plan: DraftPlan
    approval_hash: Annotated[
        str,
        StringConstraints(strict=True, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"),
    ]
    approval_method: Literal["explicit_user_confirmation"]

    @classmethod
    def from_explicit_confirmation(cls, draft_plan: DraftPlan) -> ApprovedPlan:
        """Create the confirmation receipt after a user accepts this exact preview."""
        if draft_plan.ambiguities:
            raise ValueError("a plan with unresolved ambiguities cannot be approved")
        if draft_plan.support_mode in {
            SupportMode.CLARIFICATION_REQUIRED,
            SupportMode.NOT_SIMULATABLE,
        }:
            raise ValueError("an unsupported plan cannot be approved")
        return cls(
            draft_plan=draft_plan,
            approval_hash=_draft_plan_hash(draft_plan),
            approval_method="explicit_user_confirmation",
        )

    @model_validator(mode="after")
    def approval_matches_preview(self) -> ApprovedPlan:
        if self.draft_plan.ambiguities:
            raise ValueError("a plan with unresolved ambiguities cannot be approved")
        if self.draft_plan.support_mode in {
            SupportMode.CLARIFICATION_REQUIRED,
            SupportMode.NOT_SIMULATABLE,
        }:
            raise ValueError("an unsupported plan cannot be approved")
        if self.approval_hash != _draft_plan_hash(self.draft_plan):
            raise ValueError("approval hash does not match the previewed plan")
        return self


def _draft_plan_hash(draft_plan: DraftPlan) -> str:
    canonical = json.dumps(
        draft_plan.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MultiPlanAlternative(StrictContract):
    """One named alternative produced from the same paused source Branch."""

    variant_id: StableId
    label: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)]
    plan: DraftPlan


class MultiPlanDraft(StrictContract):
    """An editable, read-only preview of sibling plans sharing one origin and fork time."""

    schema_version: Literal[1] = 1
    branch_origin: StableId
    source_state_hash: Annotated[
        str,
        StringConstraints(strict=True, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"),
    ]
    fork_at_minute: NonNegativeMinute
    parser_provenance: CompilerProvenance
    branches: list[MultiPlanAlternative] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def sibling_plans_share_origin_and_time(self) -> MultiPlanDraft:
        if len({branch.variant_id for branch in self.branches}) != len(self.branches):
            raise ValueError("multi-plan variant IDs must be unique")
        if len({branch.label for branch in self.branches}) != len(self.branches):
            raise ValueError("multi-plan branch labels must be unique")
        for branch in self.branches:
            if branch.plan.branch_id != self.branch_origin:
                raise ValueError("every alternative must use the declared branch_origin")
            if branch.plan.fork_at_minute != self.fork_at_minute:
                raise ValueError("every alternative must use the same fork_at_minute")
            if branch.plan.compiler_provenance != self.parser_provenance:
                raise ValueError("parser_provenance must match every alternative plan")
        return self


class MultiPlanApproval(StrictContract):
    """Explicit confirmation receipt bound to the complete set of edited alternatives."""

    multi_plan: MultiPlanDraft
    approval_hash: Annotated[
        str,
        StringConstraints(strict=True, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"),
    ]
    approval_method: Literal["explicit_user_confirmation"]

    @classmethod
    def from_explicit_confirmation(cls, multi_plan: MultiPlanDraft) -> MultiPlanApproval:
        """Create a receipt only for a complete, executable, nonduplicate branch set."""
        _validate_multi_plan_approval(multi_plan)
        return cls(
            multi_plan=multi_plan,
            approval_hash=_multi_plan_hash(multi_plan),
            approval_method="explicit_user_confirmation",
        )

    @model_validator(mode="after")
    def approval_matches_preview(self) -> MultiPlanApproval:
        _validate_multi_plan_approval(self.multi_plan)
        if self.approval_hash != _multi_plan_hash(self.multi_plan):
            raise ValueError("approval hash does not match the complete multi-plan preview")
        return self


def _validate_multi_plan_approval(multi_plan: MultiPlanDraft) -> None:
    if len(multi_plan.branches) < 2:
        raise ValueError("a multi-plan needs at least two alternative branches")
    signatures = set()
    for branch in multi_plan.branches:
        plan = branch.plan
        if plan.ambiguities:
            raise ValueError(f"alternative {branch.variant_id!r} has unresolved ambiguities")
        if plan.support_mode in {
            SupportMode.CLARIFICATION_REQUIRED,
            SupportMode.NOT_SIMULATABLE,
        }:
            raise ValueError(f"alternative {branch.variant_id!r} is not executable")
        signature = multi_plan_effect_signature(plan)
        if signature in signatures:
            raise ValueError("multi-plan alternatives must have different modeled effects")
        signatures.add(signature)


def multi_plan_effect_signature(plan: DraftPlan) -> str:
    """Hash only executable effect fields so IDs/labels cannot make a duplicate look new."""
    effects = []
    for effect in plan.effects:
        effects.append(
            {
                "op": effect.op,
                "target_selector": effect.target_selector.model_dump(mode="json"),
                "start_minute": effect.start_minute,
                "end_minute": effect.end_minute,
                "guard": effect.guard.model_dump(mode="json") if effect.guard else None,
                "parameters": effect.parameters,
                "units": effect.units,
            }
        )
    return _canonical_json({"fork_at_minute": plan.fork_at_minute, "effects": effects})


def _multi_plan_hash(multi_plan: MultiPlanDraft) -> str:
    return hashlib.sha256(_canonical_json(multi_plan.model_dump(mode="json")).encode()).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class CompilerUsage(StrictContract):
    """Bounded, prompt-free usage metadata for one adaptive compiler result."""

    model_id: StableId | None = None
    prompt_version: StableId
    prompt_hash: Annotated[str, StringConstraints(strict=True, min_length=64, max_length=64)]
    input_hash: Annotated[str, StringConstraints(strict=True, min_length=64, max_length=64)]
    api_request_count: Annotated[int, Field(strict=True, ge=0, le=2)]
    input_tokens: Annotated[int, Field(strict=True, ge=0)] = 0
    output_tokens: Annotated[int, Field(strict=True, ge=0)] = 0
    estimated_cost_usd: Annotated[float, Field(ge=0.0)] = 0.0
    cache_hit: bool = False
    error_kind: StableId | None = None


class CompilerResult(StrictContract):
    """Truthful compiler preview; it never applies the returned proposal."""

    status: Literal["compiled", "clarification_required", "not_simulatable"]
    support_mode: SupportMode
    draft_plan: DraftPlan | None = None
    mechanism_summary: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1000)]
    limitations: list[
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    ] = Field(default_factory=list, max_length=50)
    questions: list[
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    ] = Field(default_factory=list, max_length=10)
    choices: list[StableId] = Field(default_factory=list, max_length=100)
    preview_notice: str = "これは作用のプレビューで、シミュレーションの観測結果ではありません。"
    fallback_used: bool = False
    usage: CompilerUsage

    @model_validator(mode="after")
    def result_matches_support_mode(self) -> CompilerResult:
        if self.status == "compiled":
            if self.draft_plan is None or self.support_mode not in {
                SupportMode.NATIVE,
                SupportMode.COMPOSED,
                SupportMode.APPROXIMATE,
            }:
                raise ValueError("compiled results require an executable preview plan")
            if self.draft_plan.support_mode is not self.support_mode:
                raise ValueError("result and DraftPlan support_mode must match")
        elif self.draft_plan is not None:
            raise ValueError("uncompiled results cannot include an executable DraftPlan")
        if self.status == "clarification_required":
            if self.support_mode is not SupportMode.CLARIFICATION_REQUIRED:
                raise ValueError("clarification results require clarification_required mode")
            if not self.questions and not self.choices:
                raise ValueError("clarification results require a question or choices")
        if self.status == "not_simulatable":
            if self.support_mode is not SupportMode.NOT_SIMULATABLE or not self.limitations:
                raise ValueError("not_simulatable results must explain their limitations")
        return self


class ParseIssue(StrictContract):
    code: StableId
    message: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    choices: list[StableId] = Field(default_factory=list, max_length=100)


class ParseResult(StrictContract):
    status: Literal["parsed", "clarification_required", "rejected"]
    draft_plan: DraftPlan | None = None
    issues: list[ParseIssue] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def result_shape_matches_status(self) -> ParseResult:
        if self.status == "rejected" and self.draft_plan is not None:
            raise ValueError("rejected parse results cannot include a DraftPlan")
        if self.status == "parsed" and self.draft_plan is None:
            raise ValueError("parsed results require a DraftPlan")
        if self.status == "clarification_required" and (
            self.draft_plan is None or not self.draft_plan.ambiguities
        ):
            raise ValueError("clarification results require an ambiguous DraftPlan")
        return self


SUPPORT_ADVICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "priority": {"type": "integer", "enum": [0, 1]},
        "rationale": {"type": "string", "maxLength": 160},
    },
    "required": ["priority", "rationale"],
    "additionalProperties": False,
}

SOCIAL_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["offer_reassurance", "share_practical_information", "no_interaction"],
        }
    },
    "required": ["action"],
    "additionalProperties": False,
}


def social_interaction_proposal_schema(
    *, event_id: str, allowed_actions: list[str], reason_codes: list[str]
) -> dict[str, Any]:
    """Build a closed proposal contract for one rule-selected interaction candidate."""
    if not event_id or not allowed_actions or not reason_codes:
        raise ValueError("social interaction schemas require an event ID and choices")
    if len(set(allowed_actions)) != len(allowed_actions) or len(set(reason_codes)) != len(
        reason_codes
    ):
        raise ValueError("social interaction schema choices must be unique")
    return {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "enum": [event_id]},
            "action": {"type": "string", "enum": allowed_actions},
            "reason_short": {"type": "string", "enum": reason_codes},
        },
        "required": ["event_id", "action", "reason_short"],
        "additionalProperties": False,
    }


def initial_needs_schema(profile_count: int) -> dict[str, Any]:
    """Return the exact-count output contract for Opus synthetic need generation."""
    if profile_count <= 0:
        raise ValueError("profile_count must be positive")
    needs = {
        "type": "object",
        "properties": {
            "toilet_urgency": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "hunger": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "fatigue": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        "required": ["toilet_urgency", "hunger", "fatigue"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "profiles": {
                "type": "array",
                "minItems": profile_count,
                "maxItems": profile_count,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "maxLength": 64},
                        "initial_needs": needs,
                    },
                    "required": ["id", "initial_needs"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["profiles"],
        "additionalProperties": False,
    }


def scenario_draft_schema(scenario: ScenarioSpec, *, max_events: int = 20) -> dict[str, Any]:
    """Return an ID-bounded schema for synthetic profile and facility-event drafts."""
    profile_count = len(scenario.profiles)
    if profile_count <= 0:
        raise ValueError("scenario generation requires at least one profile")
    if max_events < 0:
        raise ValueError("max_events cannot be negative")
    zone_indexes = list(range(len(scenario.zones)))
    facility_indexes = list(range(len(scenario.facilities)))
    maximum_capacity = max((facility.capacity for facility in scenario.facilities), default=1)
    profile_item = {
        "type": "object",
        "properties": {
            "profile_index": {"type": "integer", "minimum": 0, "maximum": profile_count - 1},
            "family_index": {
                "type": "integer",
                "minimum": 0,
                "maximum": profile_count - 1,
            },
            "zone_index": {"type": "integer", "enum": zone_indexes},
            "mobility": {"type": "string", "enum": [item.value for item in Mobility]},
            "support_need": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "stress_sensitivity": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "initial_needs": {
                "type": "object",
                "properties": {
                    "toilet_urgency": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "hunger": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "fatigue": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                "required": ["toilet_urgency", "hunger", "fatigue"],
                "additionalProperties": False,
            },
        },
        "required": [
            "profile_index",
            "family_index",
            "zone_index",
            "mobility",
            "support_need",
            "stress_sensitivity",
            "initial_needs",
        ],
        "additionalProperties": False,
    }
    event_item = {
        "type": "object",
        "properties": {
            "event_type": {
                "type": "string",
                "enum": ["facility_outage", "facility_recovery"],
            },
            "due_minute": {
                "type": "integer",
                "minimum": 0,
                "maximum": scenario.duration_minutes - 1,
            },
            "facility_index": {"type": "integer", "enum": facility_indexes},
            "units": {"type": "integer", "minimum": 1, "maximum": maximum_capacity},
        },
        "required": ["event_type", "due_minute", "facility_index", "units"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "profiles": {
                "type": "array",
                "minItems": profile_count,
                "maxItems": profile_count,
                "items": profile_item,
            },
            "events": {
                "type": "array",
                "minItems": 0,
                "maxItems": max_events,
                "items": event_item,
            },
        },
        "required": ["profiles", "events"],
        "additionalProperties": False,
    }
