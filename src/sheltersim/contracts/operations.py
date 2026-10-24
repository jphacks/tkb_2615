"""Versioned shared contracts for action, support and shelter operations.

These schemas define record and state boundaries. They do not implement care outcomes,
staff scheduling, facility repair physics, or inferred resource demand.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import ExtensionModel, Finite, Identifier, Point2D

SHARED_OPERATIONS_KEY = "shared_operations_v1"
SHARED_OPERATIONS_POLICY = "shared-operations-v1"
MAX_SHARED_OPERATION_HISTORY = 20_000


class SharedOperationsConfig(ExtensionModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[SHARED_OPERATIONS_POLICY] = SHARED_OPERATIONS_POLICY


class NumericStateChange(ExtensionModel):
    """Observed numeric state before/after an applied operation, with explicit units."""

    entity_id: Identifier
    entity_kind: Literal["agent", "staff", "facility", "zone", "resource", "model_state"]
    field_path: str = Field(pattern=r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){0,5}$", max_length=128)
    before: Finite
    after: Finite
    unit: str = Field(min_length=1, max_length=80)

class ActionLifecycleReceipt(ExtensionModel):
    """The existing event-log wire shape for one applied action transition."""

    schema_version: Literal[1] = 1
    action_id: Identifier
    agent_id: Identifier
    action: Identifier
    target_id: Identifier | None = None
    transition: Literal["started", "continued", "completed", "interrupted", "abandoned", "censored"]
    start_minute: int = Field(strict=True, ge=0)
    transition_minute: int = Field(strict=True, ge=0)
    duration_minutes: int = Field(strict=True, ge=0)
    reason_code: str = Field(min_length=1, max_length=120)
    activity: str | None = Field(default=None, max_length=80)
    start_activity: str | None = Field(default=None, max_length=80)
    start_needs: dict[str, Finite] = Field(default_factory=dict)
    current_needs: dict[str, Finite] = Field(default_factory=dict)
    model_id: Identifier
    model_source_id: Identifier

    @model_validator(mode="after")
    def receipt_time_is_consistent(self) -> ActionLifecycleReceipt:
        if self.transition_minute < self.start_minute:
            raise ValueError("action transition cannot precede its start")
        if self.duration_minutes != self.transition_minute - self.start_minute:
            raise ValueError("action duration must equal transition_minute - start_minute")
        return self


SupportKind = Literal[
    "medical_assessment", "medication_support", "caregiver_assistance",
    "privacy_accommodation", "mobility_assistance", "hygiene_support",
    "emotional_support", "language_access", "dependent_assistance",
]


class CareTransitionReceipt(ExtensionModel):
    record_type: Literal["care"] = "care"
    schema_version: Literal[1] = 1
    event_type: Literal["care_support_transition"] = "care_support_transition"
    event_id: Identifier
    request_id: Identifier
    agent_id: Identifier
    household_id: Identifier | None = None
    support_kind: SupportKind
    status: Literal["requested", "assigned", "started", "completed", "interrupted", "declined"]
    minute: int = Field(strict=True, ge=0)
    staff_id: Identifier | None = None
    outcome_observation: Literal["not_applicable", "observed_change", "observed_no_change", "not_measured"] = "not_applicable"
    state_changes: tuple[NumericStateChange, ...] = Field(default=(), max_length=32)
    causation_id: Identifier | None = None
    source_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def result_evidence_matches_transition(self) -> CareTransitionReceipt:
        if self.status == "completed":
            if self.outcome_observation == "observed_change" and not self.state_changes:
                raise ValueError("observed_change requires recorded state deltas")
            if self.outcome_observation == "observed_change" and not any(
                row.before != row.after for row in self.state_changes
            ):
                raise ValueError("observed_change requires at least one changed value")
            if self.outcome_observation == "observed_no_change" and (
                not self.state_changes or any(row.before != row.after for row in self.state_changes)
            ):
                raise ValueError("observed_no_change requires only measured unchanged values")
            if self.outcome_observation == "not_measured" and self.state_changes:
                raise ValueError("state values require an observed outcome")
            if self.outcome_observation == "not_applicable" and self.state_changes:
                raise ValueError("state deltas require outcome_observation=observed_change")
        elif self.outcome_observation != "not_applicable" or self.state_changes:
            raise ValueError("only a completed support operation may report an outcome")
        return self


class StaffSupportContinuityReceipt(ExtensionModel):
    """Evidence that an interrupted staff task requeued or closed resident support."""

    record_type: Literal["care"] = "care"
    schema_version: Literal[1] = 1
    event_type: Literal["staff_support_continuity"] = "staff_support_continuity"
    event_id: Identifier
    request_id: Identifier
    agent_id: Identifier
    task_id: Identifier
    previous_staff_id: Identifier
    status: Literal["requeued", "unresolved"]
    cause_type: Literal[
        "staff_unavailable", "staff_shift_change", "route_unreachable"
    ] = "staff_unavailable"
    minute: int = Field(strict=True, ge=0)
    causation_id: Identifier
    source_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def only_support_continuity_is_recorded(self) -> StaffSupportContinuityReceipt:
        if self.event_id == self.causation_id:
            raise ValueError("support continuity event cannot cause itself")
        return self


class StaffHandoffReceipt(ExtensionModel):
    record_type: Literal["operations"] = "operations"
    schema_version: Literal[1] = 1
    event_type: Literal["staff_handoff"] = "staff_handoff"
    event_id: Identifier
    task_id: Identifier
    previous_staff_id: Identifier
    replacement_staff_id: Identifier | None = None
    status: Literal["reassigned", "unfilled", "rejected"]
    minute: int = Field(strict=True, ge=0)
    reason_code: Identifier
    causation_id: Identifier | None = None
    source_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def status_matches_assignment(self) -> StaffHandoffReceipt:
        if (self.status == "reassigned") != (self.replacement_staff_id is not None):
            raise ValueError("reassigned handoffs require one replacement staff ID")
        if self.replacement_staff_id == self.previous_staff_id:
            raise ValueError("a staff handoff must change the assigned person")
        return self


class FacilityRecoveryReceipt(ExtensionModel):
    record_type: Literal["operations"] = "operations"
    schema_version: Literal[1] = 1
    event_type: Literal["facility_recovery_transition"] = "facility_recovery_transition"
    event_id: Identifier
    facility_id: Identifier
    component_id: Identifier
    status: Literal["degraded", "outage", "repair_started", "repaired", "restored"]
    minute: int = Field(strict=True, ge=0)
    previous_working_units: int = Field(strict=True, ge=0)
    working_units: int = Field(strict=True, ge=0)
    source_ids: tuple[Identifier, ...] = ()
    causation_id: Identifier | None = None

    @model_validator(mode="after")
    def recovery_record_changes_capacity(self) -> FacilityRecoveryReceipt:
        if self.status in {"degraded", "outage", "restored"} and self.working_units == self.previous_working_units:
            raise ValueError("capacity transition must record different before/after working units")
        return self


class StaffOperationReceipt(ExtensionModel):
    """Applied evidence for one autonomous staff task completion."""

    record_type: Literal["operations"] = "operations"
    schema_version: Literal[1] = 1
    event_type: Literal["staff_operation_transition"] = "staff_operation_transition"
    event_id: Identifier
    task_id: Identifier
    staff_id: Identifier
    task_kind: Literal[
        "cleaning", "maintenance", "information_outreach", "intake", "patrol"
    ]
    target_id: Identifier | None = None
    target_zone_id: Identifier
    reason: str = Field(min_length=1, max_length=240)
    status: Literal["completed", "blocked"]
    minute: int = Field(strict=True, ge=0)
    start_minute: int = Field(strict=True, ge=0)
    arrival_minute: int = Field(strict=True, ge=0)
    travel_minutes: int = Field(strict=True, ge=0)
    outcome_observation: Literal[
        "observed_change", "observed_no_change", "not_measured"
    ]
    state_changes: tuple[NumericStateChange, ...] = Field(default=(), max_length=256)
    total_state_change_count: int = Field(strict=True, ge=0)
    state_changes_truncated: bool = False
    causation_id: Identifier
    source_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def completion_evidence_is_consistent(self) -> StaffOperationReceipt:
        if not self.start_minute <= self.arrival_minute <= self.minute:
            raise ValueError("staff operation completion times are inconsistent")
        if self.travel_minutes > self.arrival_minute - self.start_minute:
            raise ValueError("staff operation travel exceeds elapsed assignment time")
        if self.total_state_change_count < len(self.state_changes):
            raise ValueError("staff operation change count is smaller than its sample")
        if self.state_changes_truncated != (
            self.total_state_change_count > len(self.state_changes)
        ):
            raise ValueError("staff operation change truncation flag is inconsistent")
        if self.status == "blocked":
            if self.state_changes or self.total_state_change_count:
                raise ValueError("blocked staff operations cannot report state changes")
            if self.outcome_observation != "observed_no_change":
                raise ValueError(
                    "blocked staff operation must record its observed no-change outcome"
                )
        elif self.outcome_observation == "observed_change":
            if self.total_state_change_count == 0 or not any(
                row.before != row.after for row in self.state_changes
            ):
                raise ValueError("observed staff operation changes require a measured delta")
        elif self.outcome_observation == "observed_no_change":
            if self.total_state_change_count != 0 or any(
                row.before != row.after for row in self.state_changes
            ):
                raise ValueError("observed no-change staff operation contains a changed value")
        elif self.state_changes or self.total_state_change_count:
            raise ValueError("unmeasured staff operation cannot contain state deltas")
        return self


class FacilityQueueTransitionReceipt(ExtensionModel):
    """Event linked queue lifecycle evidence for one independent service point."""

    record_type: Literal["operations"] = "operations"
    schema_version: Literal[1] = 1
    event_type: Literal["facility_queue_transition"] = "facility_queue_transition"
    event_id: Identifier
    request_id: Identifier
    queue_id: Identifier
    service_point_id: Identifier
    facility_id: Identifier
    agent_id: Identifier
    service_kind: Literal["toilet", "water", "meal"]
    transition: Literal["queued", "service_started", "completed", "cancelled", "interrupted", "requeued", "abandoned"]
    minute: int = Field(strict=True, ge=0)
    requested_minute: int = Field(strict=True, ge=0)
    task_id: Identifier
    queue_position: int | None = Field(default=None, strict=True, ge=1)
    service_capacity_concurrent: int | None = Field(default=None, strict=True, ge=0)
    wait_minutes: int | None = Field(default=None, strict=True, ge=0)
    causation_id: Identifier | None = None

    @model_validator(mode="after")
    def queue_transition_time_is_valid(self) -> FacilityQueueTransitionReceipt:
        if self.requested_minute > self.minute:
            raise ValueError("facility queue request cannot originate in the future")
        if self.wait_minutes is not None and self.wait_minutes > self.minute - self.requested_minute:
            raise ValueError("queue wait cannot exceed elapsed time since request")
        if self.transition in {"queued", "requeued"} and self.queue_position is None:
            raise ValueError("queued transitions require a queue position")
        return self


OperationHistoryRecord = Annotated[
    Union[
        CareTransitionReceipt,
        StaffSupportContinuityReceipt,
        StaffHandoffReceipt,
        FacilityRecoveryReceipt,
        StaffOperationReceipt,
    ],
    Field(discriminator="event_type"),
]


class CareRequestState(ExtensionModel):
    request_id: Identifier
    agent_id: Identifier
    household_id: Identifier | None = None
    support_kind: SupportKind
    status: Literal["requested", "assigned", "started", "completed", "interrupted", "declined"]
    created_minute: int = Field(strict=True, ge=0)
    updated_minute: int = Field(strict=True, ge=0)
    latest_event_id: Identifier

    @model_validator(mode="after")
    def request_time_is_monotonic(self) -> CareRequestState:
        if self.updated_minute < self.created_minute:
            raise ValueError("care request update cannot precede its creation")
        return self


class SharedOperationsState(ExtensionModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[SHARED_OPERATIONS_POLICY]
    support_requests: dict[Identifier, CareRequestState] = Field(default_factory=dict, max_length=100_000)
    history: list[OperationHistoryRecord] = Field(default_factory=list, max_length=MAX_SHARED_OPERATION_HISTORY)
    history_truncated: bool = False

    @model_validator(mode="after")
    def support_request_projection_contains_only_active_requests(self) -> SharedOperationsState:
        if any(key != row.request_id or row.status not in {"requested", "assigned", "started"}
               for key, row in self.support_requests.items()):
            raise ValueError("support_requests contains an invalid or terminal request")
        return self


class OperationalDemandSnapshot(ExtensionModel):
    """Point-in-time observed state, not an estimate of unmet need or future demand."""

    schema_version: Literal[1] = 1
    minute: int = Field(strict=True, ge=0)
    time_semantics: Literal["before_tick"] = "before_tick"
    coverage: Literal["observed_state_only"] = "observed_state_only"
    population_count: int = Field(strict=True, ge=0)
    pending_arrivals: int = Field(strict=True, ge=0)
    zones: tuple[dict[str, Any], ...] = ()
    services: tuple[dict[str, Any], ...] = ()
    staff: dict[str, int]
    resources: dict[str, int]
    support_requests: dict[str, int]
    unestimated: tuple[str, ...] = (
        "future_resource_demand",
        "required_bed_count",
        "unmet_service_need",
        "required_staff_coverage",
    )


# Typed parameter models for the next executable EffectProgram primitives. They are
# discoverable schemas only until registered with an installed handler in effects.py.
class TemporaryRoomGeometry(ExtensionModel):
    schema_version: Literal[1] = 1
    room_id: Identifier
    zone_id: Identifier
    polygon_m: tuple[Point2D, ...] = Field(min_length=3, max_length=64)
    people_capacity: int = Field(strict=True, gt=0, le=1_000_000)
    sleeping_capacity: int = Field(strict=True, ge=0, le=1_000_000)
    bed_points_m: tuple[Point2D, ...] = Field(default=(), max_length=100_000)
    route_edge_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=10_000)
    source_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def capacity_relationship_is_valid(self) -> TemporaryRoomGeometry:
        from sheltersim.contracts.extensions import point_in_polygon, polygon_area

        if not _is_simple_polygon(self.polygon_m) or polygon_area(self.polygon_m) <= 0:
            raise ValueError("temporary room polygon must be simple and have nonzero area")
        if self.sleeping_capacity > self.people_capacity:
            raise ValueError("temporary room sleeping capacity cannot exceed people capacity")
        if self.sleeping_capacity and len(self.bed_points_m) != self.sleeping_capacity:
            raise ValueError("a physical sleeping room requires one explicit bed point per sleeping place")
        if self.sleeping_capacity == 0 and self.bed_points_m:
            raise ValueError("bed points require a positive sleeping capacity")
        if any(not _strictly_inside(point, self.polygon_m, point_in_polygon) for point in self.bed_points_m):
            raise ValueError("temporary room bed points must lie strictly inside its polygon")
        if len(self.route_edge_ids) != len(set(self.route_edge_ids)):
            raise ValueError("temporary room route edge IDs must be unique")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("temporary room source IDs must be unique")
        return self


class EquipmentTransferParameters(ExtensionModel):
    schema_version: Literal[1] = 1
    equipment_id: Identifier
    from_zone_id: Identifier
    to_zone_id: Identifier
    point_m: Point2D
    source_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def transfer_changes_zone(self) -> EquipmentTransferParameters:
        if self.from_zone_id == self.to_zone_id:
            raise ValueError("equipment transfer must select a different destination zone")
        return self


class StaffHandoffParameters(ExtensionModel):
    schema_version: Literal[1] = 1
    staff_id: Identifier
    replacement_staff_id: Identifier | None = None
    task_id: Identifier
    reason_code: Identifier

    @model_validator(mode="after")
    def handoff_changes_staff(self) -> StaffHandoffParameters:
        if self.replacement_staff_id == self.staff_id:
            raise ValueError("staff handoff replacement must be a different person")
        return self


class FacilityRepairParameters(ExtensionModel):
    schema_version: Literal[1] = 1
    facility_id: Identifier
    component_id: Identifier
    expected_working_units: int = Field(strict=True, ge=0)
    restored_working_units: int = Field(strict=True, ge=0)
    source_ids: tuple[Identifier, ...] = ()


def validate_shared_operations_config(raw: Any) -> SharedOperationsConfig:
    try:
        return SharedOperationsConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid shared_operations config") from exc


def _is_simple_polygon(points: tuple[Point2D, ...]) -> bool:
    """Reject zero edges and crossings without accepting a self-intersecting room."""
    edges = tuple(zip(points, (*points[1:], points[0])))
    for index, (a, b) in enumerate(edges):
        if a == b:
            return False
        for other_index, (c, d) in enumerate(edges[index + 1 :], index + 1):
            if other_index in {index + 1, len(edges) - 1 if index == 0 else -1}:
                continue
            if _segments_intersect(a, b, c, d):
                return False
    return True


def _segments_intersect(a: Point2D, b: Point2D, c: Point2D, d: Point2D) -> bool:
    epsilon = 1e-9

    def cross(first: Point2D, second: Point2D, third: Point2D) -> float:
        return (second.x - first.x) * (third.y - first.y) - (second.y - first.y) * (third.x - first.x)

    def on_segment(first: Point2D, second: Point2D, point: Point2D) -> bool:
        return (min(first.x, second.x) - epsilon <= point.x <= max(first.x, second.x) + epsilon
                and min(first.y, second.y) - epsilon <= point.y <= max(first.y, second.y) + epsilon)

    ab_c, ab_d = cross(a, b, c), cross(a, b, d)
    cd_a, cd_b = cross(c, d, a), cross(c, d, b)
    if ((ab_c > epsilon and ab_d < -epsilon) or (ab_c < -epsilon and ab_d > epsilon)) and (
        (cd_a > epsilon and cd_b < -epsilon) or (cd_a < -epsilon and cd_b > epsilon)
    ):
        return True
    return ((abs(ab_c) <= epsilon and on_segment(a, b, c))
            or (abs(ab_d) <= epsilon and on_segment(a, b, d))
            or (abs(cd_a) <= epsilon and on_segment(c, d, a))
            or (abs(cd_b) <= epsilon and on_segment(c, d, b)))


def _strictly_inside(point: Point2D, polygon: tuple[Point2D, ...], point_in_polygon: Any) -> bool:
    if not point_in_polygon(point, polygon):
        return False
    return not any(_segments_intersect(point, point, a, b) for a, b in zip(polygon, (*polygon[1:], polygon[0])))
