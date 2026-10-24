"""Pure per-minute KPI snapshots for reproducible run inspection."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from sheltersim.core.world import WorldState
from sheltersim.domain.models import SupportRequestStatus
from sheltersim.domain.resources import RESOURCE_METRIC_FIELDS, resource_metric_values
from sheltersim.domain.staff import (
    support_required_skills,
    support_wait_reason_codes,
    support_waited_minutes,
)


@dataclass(frozen=True)
class MetricSnapshot:
    """Aggregated metrics captured without mutating simulation state."""

    minute_index: int
    toilet_queue_length: int
    toilet_active_users: int
    toilet_working_units: int
    toilet_wait_count: float
    toilet_wait_total_minutes: float
    toilet_wait_mean_minutes: float
    toilet_wait_max_minutes: float
    stress_mean: float
    stress_high_count: int
    staff_assigned_task_count: int
    staff_busy_count: int
    staff_load_ratio: float
    unresolved_support_request_count: int
    source_ids_json: str
    support_no_qualified_worker_count: int = 0
    support_no_qualified_worker_request_minutes: float = 0.0
    support_fairness_promoted_assignment_count: float = 0.0
    support_wait_staff_busy_request_minutes: float = 0.0
    support_wait_staff_off_shift_request_minutes: float = 0.0

    @classmethod
    def from_world(cls, world: WorldState, *, minute: int | None = None) -> MetricSnapshot:
        """Measure queue, wait, stress, staff load and pending help from current state."""
        sample_minute = world.minute_index if minute is None else minute
        if sample_minute != world.minute_index:
            raise ValueError("metric minute must match WorldState")
        toilets = [
            facility
            for facility in world.facilities.values()
            if "toilet" in facility.kind.casefold()
        ]
        queued_ids = sorted(
            agent_id for facility in toilets for agent_id in facility.queue_agent_ids
        )
        active_ids = sorted(
            agent_id for facility in toilets for agent_id in facility.active_agent_ids
        )
        stresses = [agent.stress for agent in world.agents.values()]
        high_stress_ids = sorted(agent.id for agent in world.agents.values() if agent.stress >= 0.7)
        active_support_agent_ids = sorted(
            request.agent_id
            for request in world.support_requests.values()
            if request.status in (SupportRequestStatus.PENDING, SupportRequestStatus.ASSIGNED)
        )
        staff_task_ids = sorted(
            task_id for staff in world.staff.values() for task_id in staff.work_queue
        )
        busy_staff_ids = sorted(
            staff.id
            for staff in world.staff.values()
            if any(
                world.active_tasks[task_id].start_minute
                <= sample_minute
                < world.active_tasks[task_id].due_minute
                for task_id in staff.work_queue
            )
        )
        pending_support = [
            request
            for request in world.support_requests.values()
            if request.status is SupportRequestStatus.PENDING
        ]
        active_support = [
            request
            for request in world.support_requests.values()
            if request.status in (SupportRequestStatus.PENDING, SupportRequestStatus.ASSIGNED)
        ]
        support_wait_details = {
            request.agent_id: {
                "request_id": request.id,
                "waited_minutes": support_waited_minutes(
                    world, request.id, minute=sample_minute
                ),
                "reason_codes": list(
                    support_wait_reason_codes(world, request.id, minute=sample_minute)
                ),
            }
            for request in sorted(active_support, key=lambda item: item.agent_id)
        }
        no_qualified_support_ids = sorted(
            request.agent_id
            for request in pending_support
            if not any(
                support_required_skills(world, request.id).intersection(staff.skills)
                for staff in world.staff.values()
            )
        )
        wait_count = world.metric_state.get("toilet_wait_count", 0.0)
        wait_total = world.metric_state.get("toilet_wait_total_minutes", 0.0)
        wait_max = world.metric_state.get("toilet_wait_max_minutes", 0.0)
        source_events = sorted(
            record.event.event_id
            for record in world.event_log.at_minute(sample_minute)
        )
        source_ids = {
            "toilet_queue_agents": queued_ids,
            "toilet_active_agents": active_ids,
            "high_stress_agents": high_stress_ids,
            "staff_tasks": staff_task_ids,
            "busy_staff": busy_staff_ids,
            "unresolved_support_agents": active_support_agent_ids,
            "support_requests_without_qualified_staff": no_qualified_support_ids,
            "support_wait_by_agent": support_wait_details,
            "support_wait_exposure_semantics": (
                "per-minute observed request exposure by blocker; reason totals may overlap "
                "and are not exclusive causal attribution"
            ),
            "realized_events": source_events,
        }
        return cls(
            minute_index=sample_minute,
            toilet_queue_length=len(queued_ids),
            toilet_active_users=len(active_ids),
            toilet_working_units=sum(facility.working_units for facility in toilets),
            toilet_wait_count=wait_count,
            toilet_wait_total_minutes=wait_total,
            toilet_wait_mean_minutes=wait_total / wait_count if wait_count else 0.0,
            toilet_wait_max_minutes=wait_max,
            stress_mean=sum(stresses) / len(stresses) if stresses else 0.0,
            stress_high_count=len(high_stress_ids),
            staff_assigned_task_count=len(staff_task_ids),
            staff_busy_count=len(busy_staff_ids),
            staff_load_ratio=len(busy_staff_ids) / len(world.staff) if world.staff else 0.0,
            unresolved_support_request_count=len(active_support_agent_ids),
            support_no_qualified_worker_count=len(no_qualified_support_ids),
            support_no_qualified_worker_request_minutes=world.metric_state.get(
                "support_no_qualified_worker_request_minutes", 0.0
            ),
            support_fairness_promoted_assignment_count=world.metric_state.get(
                "support_fairness_promoted_assignment_count", 0.0
            ),
            support_wait_staff_busy_request_minutes=world.metric_state.get(
                "support_wait_staff_busy_request_minutes", 0.0
            ),
            support_wait_staff_off_shift_request_minutes=world.metric_state.get(
                "support_wait_staff_off_shift_request_minutes", 0.0
            ),
            source_ids_json=json.dumps(
                source_ids,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def csv_row(self) -> dict[str, Any]:
        """Return a detached row with stable CSV column names."""
        return asdict(self)


class MetricsRecorder:
    """Collect one snapshot at each explicit collect-metrics phase."""

    def __init__(self) -> None:
        self.snapshots: list[MetricSnapshot] = []
        self.resource_snapshots: list[dict[str, int]] | None = None

    def capture(self, world: WorldState, *, minute: int | None = None) -> MetricSnapshot:
        snapshot = MetricSnapshot.from_world(world, minute=minute)
        if self.snapshots and snapshot.minute_index <= self.snapshots[-1].minute_index:
            raise ValueError("metric snapshots must have strictly increasing minutes")
        resource_values = resource_metric_values(world)
        if resource_values is None:
            if self.resource_snapshots is not None:
                raise ValueError("resource ledger cannot be removed during metric capture")
        else:
            if self.resource_snapshots is None:
                if self.snapshots:
                    raise ValueError("resource ledger must be enabled before metric capture")
                self.resource_snapshots = []
            self.resource_snapshots.append(
                {"minute_index": snapshot.minute_index, **resource_values}
            )
        self.snapshots.append(snapshot)
        return snapshot

    def to_dict(self) -> dict[str, Any]:
        """Serialize the complete metric accumulator for checkpoint continuation."""
        document = {"schema_version": 4, "snapshots": [asdict(item) for item in self.snapshots]}
        if self.resource_snapshots is not None:
            document["resource_snapshots"] = [dict(item) for item in self.resource_snapshots]
        return document

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> MetricsRecorder:
        """Restore legacy KPI snapshots and optional resource samples."""
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("schema_version"), int)
            or isinstance(document.get("schema_version"), bool)
            or document.get("schema_version") not in (1, 2, 3, 4)
        ):
            raise ValueError("unsupported metric accumulator schema")
        version = document["schema_version"]
        expected_fields = {"schema_version", "snapshots"}
        allowed_fields = (
            {frozenset(expected_fields)}
            if version == 1
            else {
                frozenset(expected_fields),
                frozenset(expected_fields | {"resource_snapshots"}),
            }
        )
        if frozenset(document) not in allowed_fields:
            raise ValueError(f"metric accumulator fields do not match schema v{version}")
        snapshots = document.get("snapshots")
        if not isinstance(snapshots, list):
            raise ValueError("metric accumulator snapshots must be a list")
        recorder = cls()
        try:
            restored_snapshots = []
            for item in snapshots:
                row = dict(item)
                if version < 3:
                    row.setdefault("support_no_qualified_worker_count", 0)
                    row.setdefault("support_no_qualified_worker_request_minutes", 0.0)
                    row.setdefault("support_fairness_promoted_assignment_count", 0.0)
                if version < 4:
                    row.setdefault("support_wait_staff_busy_request_minutes", 0.0)
                    row.setdefault("support_wait_staff_off_shift_request_minutes", 0.0)
                restored_snapshots.append(MetricSnapshot(**row))
            recorder.snapshots = restored_snapshots
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid metric accumulator: {exc}") from exc
        minutes = [item.minute_index for item in recorder.snapshots]
        if any(minute < 0 for minute in minutes) or minutes != sorted(set(minutes)):
            raise ValueError("metric accumulator minutes must be unique and increasing")
        if version == 1 and "resource_snapshots" in document:
            raise ValueError("metric accumulator v1 cannot contain resource snapshots")
        if "resource_snapshots" in document:
            resource_snapshots = document["resource_snapshots"]
            if not isinstance(resource_snapshots, list) or len(resource_snapshots) != len(minutes):
                raise ValueError("resource metric snapshots must align with metric snapshots")
            expected_resource_fields = {"minute_index", *RESOURCE_METRIC_FIELDS}
            for item, expected_minute in zip(resource_snapshots, minutes, strict=True):
                if not isinstance(item, dict) or set(item) != expected_resource_fields:
                    raise ValueError("resource metric snapshot fields do not match schema v1")
                if item["minute_index"] != expected_minute or any(
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                    for key, value in item.items()
                    if key != "minute_index"
                ):
                    raise ValueError("resource metric snapshot values are invalid")
            recorder.resource_snapshots = [dict(item) for item in resource_snapshots]
        return recorder
