"""Shared Run/Branch/evidence/timeline wire models for application adapters."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from sheltersim.contracts.extensions import ExtensionModel, FacilityGeometry, Identifier, Point2D
from sheltersim.contracts.operations import OperationalDemandSnapshot


class RunReference(ExtensionModel):
    schema_version: Literal[1] = 1
    project_id: Identifier
    scenario_id: Identifier
    scenario_version: int = Field(ge=1)
    scenario_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id: Identifier
    seed: int = Field(ge=0)
    model_versions: dict[str, str] = Field(default_factory=dict)


class BranchReference(ExtensionModel):
    schema_version: Literal[1] = 1
    run_id: Identifier
    branch_id: Identifier
    parent_branch_id: Identifier | None = None
    fork_minute: int = Field(ge=0)
    computed_until: int = Field(ge=0)
    checkpoint_hash: str | None = None


class AgentPosition(ExtensionModel):
    agent_id: Identifier
    zone_id: Identifier
    point: Point2D | None = None
    edge_id: Identifier | None = None
    progress: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    position_kind: Literal["physical", "display_only", "unavailable"] = "unavailable"


class ArrivalApproachPosition(ExtensionModel):
    agent_id: Identifier
    route_id: Identifier
    destination_zone_id: Identifier
    position: Point2D
    progress: float = Field(ge=0, le=1, allow_inf_nan=False)
    status: Literal["approaching", "awaiting_admission", "censored"]
    arrival_minute: int = Field(ge=0)


class EvidenceReference(ExtensionModel):
    run_id: Identifier
    branch_id: Identifier
    minute: int = Field(ge=0)
    kind: Literal["event", "kpi", "model", "source", "missing", "agent", "comparison"]
    reference_id: str = Field(min_length=1, max_length=128)
    status: Literal["recorded", "assumption", "hypothesis", "missing"]


class TimelineFrame(ExtensionModel):
    schema_version: Literal[1] = 1
    run_id: Identifier
    branch_id: Identifier
    minute: int = Field(ge=0)
    computed_until: int = Field(ge=0)
    state_hash: str
    exact: Literal[True] = True
    agents: tuple[AgentPosition, ...] = ()
    incoming_arrivals: tuple[ArrivalApproachPosition, ...] = ()
    kpis: dict[str, Any] = Field(default_factory=dict)
    events: tuple[dict[str, Any], ...] = ()
    geometry: FacilityGeometry | None = None
    people: tuple[dict[str, Any], ...] = ()
    facilities: tuple[dict[str, Any], ...] = ()
    staff: tuple[dict[str, Any], ...] = ()
    zones: tuple[dict[str, Any], ...] = ()
    social: dict[str, Any] = Field(default_factory=dict)
    movement: dict[str, Any] = Field(default_factory=dict)
    occupancy: dict[str, Any] = Field(default_factory=dict)
    operational_demand: OperationalDemandSnapshot | None = None
    spatial_constraints: dict[str, Any] | None = None
    presence: dict[str, Any] = Field(default_factory=dict)
    geometry_overlay: dict[str, Any] = Field(default_factory=dict)
    sanitation: dict[str, Any] = Field(default_factory=dict)
    recorded_history: dict[str, Any] = Field(default_factory=dict)
    pending_arrivals: int = Field(default=0, ge=0)
    kpi_time_semantics: Literal["state at boundary before this tick"] = "state at boundary before this tick"
    position_limitations: tuple[str, ...] = ()
