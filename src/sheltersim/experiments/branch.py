"""Checkpoint-backed branches and atomic, typed simulation interventions."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar, TypeAlias

from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.world import WorldState
from sheltersim.domain.models import FacilityState, StaffState
from sheltersim.domain.zones import ZoneGraph
from sheltersim.events.effects import (
    EffectProgram,
    ScheduledEffectProgram,
    has_active_facility_modifier,
    has_pending_facility_modifier,
    schedule_effect_program,
)
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.policies.space import SpaceRouter

_BRANCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class BranchError(ValueError):
    """Raised when branch lineage or an intervention is invalid."""


@dataclass(frozen=True)
class AddStaffCommand:
    """Add a staff member with an explicit stable identity and skill set."""

    staff_id: str
    zone_id: str
    skills: tuple[str, ...] = ("support",)
    kind: ClassVar[str] = "add_staff"

    def __post_init__(self) -> None:
        _validate_identifier("staff_id", self.staff_id)
        _validate_identifier("zone_id", self.zone_id)
        if not self.skills or any(not skill for skill in self.skills):
            raise BranchError("staff skills must contain non-empty values")
        if len(self.skills) != len(set(self.skills)):
            raise BranchError("staff skills must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "staff_id": self.staff_id,
            "zone_id": self.zone_id,
            "skills": list(self.skills),
        }


@dataclass(frozen=True)
class AddToiletCommand:
    """Add toilet capacity in an existing zone."""

    facility_id: str
    zone_id: str
    capacity: int = 1
    working_units: int | None = None
    kind: ClassVar[str] = "add_toilet"

    def __post_init__(self) -> None:
        _validate_identifier("facility_id", self.facility_id)
        _validate_identifier("zone_id", self.zone_id)
        working = self.capacity if self.working_units is None else self.working_units
        if (
            not isinstance(self.capacity, int)
            or isinstance(self.capacity, bool)
            or self.capacity <= 0
        ):
            raise BranchError("toilet capacity must be a positive integer")
        if (
            not isinstance(working, int)
            or isinstance(working, bool)
            or not 0 <= working <= self.capacity
        ):
            raise BranchError("working_units must be between zero and capacity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "facility_id": self.facility_id,
            "zone_id": self.zone_id,
            "capacity": self.capacity,
            "working_units": self.capacity if self.working_units is None else self.working_units,
        }


@dataclass(frozen=True)
class SetFacilityWorkingUnitsCommand:
    """Set available units; zero disables a facility without deleting its history."""

    facility_id: str
    working_units: int
    kind: ClassVar[str] = "set_facility_working_units"

    def __post_init__(self) -> None:
        _validate_identifier("facility_id", self.facility_id)
        if (
            not isinstance(self.working_units, int)
            or isinstance(self.working_units, bool)
            or self.working_units < 0
        ):
            raise BranchError("working_units must be a non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "facility_id": self.facility_id,
            "working_units": self.working_units,
        }


@dataclass(frozen=True)
class SetZoneEdgeCommand:
    """Adjust one directed route while retaining the source branch unchanged."""

    edge_id: str
    travel_minutes: int
    closed: bool
    max_agents_in_transit: int | None
    kind: ClassVar[str] = "set_zone_edge"

    def __post_init__(self) -> None:
        _validate_identifier("edge_id", self.edge_id)
        if (
            not isinstance(self.travel_minutes, int)
            or isinstance(self.travel_minutes, bool)
            or not 1 <= self.travel_minutes <= 1440
        ):
            raise BranchError("travel_minutes must be an integer from 1 to 1440")
        if not isinstance(self.closed, bool):
            raise BranchError("closed must be a boolean")
        if self.max_agents_in_transit is not None and (
            not isinstance(self.max_agents_in_transit, int)
            or isinstance(self.max_agents_in_transit, bool)
            or not 1 <= self.max_agents_in_transit <= 100
        ):
            raise BranchError("max_agents_in_transit must be null or an integer from 1 to 100")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "edge_id": self.edge_id,
            "travel_minutes": self.travel_minutes,
            "closed": self.closed,
            "max_agents_in_transit": self.max_agents_in_transit,
        }


@dataclass(frozen=True)
class RelocateFacilityCommand:
    """Move an idle facility to another modeled zone in a child branch."""

    facility_id: str
    zone_id: str
    kind: ClassVar[str] = "relocate_facility"

    def __post_init__(self) -> None:
        _validate_identifier("facility_id", self.facility_id)
        _validate_identifier("zone_id", self.zone_id)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "facility_id": self.facility_id, "zone_id": self.zone_id}


BranchCommand: TypeAlias = (
    AddStaffCommand
    | AddToiletCommand
    | SetFacilityWorkingUnitsCommand
    | SetZoneEdgeCommand
    | RelocateFacilityCommand
)


def command_from_dict(document: dict[str, Any]) -> BranchCommand:
    """Parse one strict typed command at an API or persistence boundary."""
    if not isinstance(document, dict):
        raise BranchError("intervention command must be an object")
    kind = document.get("kind")
    expected: dict[str, set[str]] = {
        "add_staff": {"kind", "staff_id", "zone_id", "skills"},
        "add_toilet": {"kind", "facility_id", "zone_id", "capacity", "working_units"},
        "set_facility_working_units": {"kind", "facility_id", "working_units"},
        "set_zone_edge": {
            "kind",
            "edge_id",
            "travel_minutes",
            "closed",
            "max_agents_in_transit",
        },
        "relocate_facility": {"kind", "facility_id", "zone_id"},
    }
    fields = expected.get(kind)
    if fields is None or set(document) != fields:
        raise BranchError("intervention command kind or fields are unsupported")
    if kind == "add_staff":
        skills = document["skills"]
        if not isinstance(skills, list) or any(not isinstance(item, str) for item in skills):
            raise BranchError("staff skills must be a list of strings")
        return AddStaffCommand(document["staff_id"], document["zone_id"], tuple(skills))
    if kind == "add_toilet":
        return AddToiletCommand(
            document["facility_id"],
            document["zone_id"],
            document["capacity"],
            document["working_units"],
        )
    if kind == "set_zone_edge":
        return SetZoneEdgeCommand(
            document["edge_id"],
            document["travel_minutes"],
            document["closed"],
            document["max_agents_in_transit"],
        )
    if kind == "relocate_facility":
        return RelocateFacilityCommand(document["facility_id"], document["zone_id"])
    return SetFacilityWorkingUnitsCommand(document["facility_id"], document["working_units"])


@dataclass(frozen=True)
class CommandReceipt:
    """Immutable audit details for one applied intervention."""

    command_id: str
    branch_id: str
    minute: int
    event_id: str
    state_hash: str


@dataclass
class BranchRuntime:
    """An isolated branch restored from a checkpoint with paired keyed randomness."""

    branch_id: str
    parent_branch_id: str | None
    world: WorldState
    metrics: MetricsRecorder = field(default_factory=MetricsRecorder)
    runtime_state: dict[str, Any] = field(default_factory=dict)
    command_log: list[dict[str, Any]] = field(default_factory=list)
    pairing_key: str = ""

    CURRENT_SCHEMA_VERSION: ClassVar[int] = 1

    def __post_init__(self) -> None:
        _validate_identifier("branch_id", self.branch_id)
        if self.parent_branch_id is not None:
            _validate_identifier("parent_branch_id", self.parent_branch_id)
        self.world.validate()
        expected_pairing = _pairing_key(self.world.scenario_id, self.world.seed)
        if self.pairing_key and self.pairing_key != expected_pairing:
            raise BranchError("branch RNG pairing key does not match scenario and seed")
        self.pairing_key = expected_pairing

    @classmethod
    def from_checkpoint(
        cls,
        checkpoint: Checkpoint,
        *,
        branch_id: str,
        parent_branch_id: str | None = None,
    ) -> BranchRuntime:
        """Create an isolated child; no mutable world structures are shared."""
        branch_id = _validate_identifier("branch_id", branch_id)
        metadata = checkpoint.runtime_state.get("branch", {})
        stored_branch_id = metadata.get("branch_id") if isinstance(metadata, dict) else None
        if parent_branch_id is None:
            stored_parent = metadata.get("parent_branch_id") if isinstance(metadata, dict) else None
            parent_branch_id = stored_parent if stored_branch_id == branch_id else stored_branch_id
        try:
            return cls(
                branch_id=branch_id,
                parent_branch_id=parent_branch_id,
                world=checkpoint.restore_world(),
                metrics=checkpoint.restore_metrics(),
                runtime_state=deepcopy(checkpoint.runtime_state),
                command_log=deepcopy(metadata.get("command_log", []))
                if isinstance(metadata, dict)
                else [],
                pairing_key=_pairing_key(
                    checkpoint.scenario_id, int(checkpoint.world_state["seed"])
                ),
            )
        except (CheckpointError, KeyError, TypeError, ValueError) as exc:
            raise BranchError(f"checkpoint cannot seed a branch: {exc}") from exc

    @classmethod
    def from_world(
        cls,
        world: WorldState,
        *,
        branch_id: str = "main",
        metrics: MetricsRecorder | None = None,
    ) -> BranchRuntime:
        """Start lineage metadata for a completed or currently running world."""
        return cls(
            branch_id=branch_id,
            parent_branch_id=None,
            world=WorldState.from_dict(world.to_dict()),
            metrics=MetricsRecorder.from_dict(metrics.to_dict()) if metrics else MetricsRecorder(),
        )

    def checkpoint(self) -> Checkpoint:
        """Save branch lineage and accepted commands inside checkpoint runtime state."""
        runtime = deepcopy(self.runtime_state)
        runtime["branch"] = {
            "schema_version": self.CURRENT_SCHEMA_VERSION,
            "branch_id": self.branch_id,
            "parent_branch_id": self.parent_branch_id,
            "pairing_key": self.pairing_key,
            "command_log": deepcopy(self.command_log),
        }
        return Checkpoint.capture(self.world, metrics=self.metrics, runtime_state=runtime)

    def fork(self, branch_id: str) -> BranchRuntime:
        """Take an exact child snapshot and preserve the paired RNG scenario key."""
        return BranchRuntime.from_checkpoint(
            self.checkpoint(), branch_id=branch_id, parent_branch_id=self.branch_id
        )

    def apply(self, command: BranchCommand) -> CommandReceipt:
        """Apply a typed command atomically and record it in the realized event log."""
        if self.world.minute_index >= self.world.duration_minutes:
            raise BranchError("cannot intervene after the simulation has completed")
        if not isinstance(
            command,
            (
                AddStaffCommand,
                AddToiletCommand,
                SetFacilityWorkingUnitsCommand,
                SetZoneEdgeCommand,
                RelocateFacilityCommand,
            ),
        ):
            raise BranchError("unsupported intervention command type")

        candidate = WorldState.from_dict(self.world.to_dict())
        _apply_to_world(candidate, command)
        sequence = candidate.allocate_event_sequence()
        event_id = f"branch-intervention-{sequence}"
        event = ScheduledEvent(
            event_id=event_id,
            kind=EventKind.INTERVENTION,
            due_minute=candidate.minute_index,
            priority=0,
            sequence=sequence,
            event_type=f"branch-{command.kind.replace('_', '-')}",
            payload=command.to_dict(),
            source_id=self.branch_id,
            provenance={"branch_id": self.branch_id, "parent_branch_id": self.parent_branch_id},
        )
        candidate.event_log.append(
            EventLogRecord(
                event=event, realized_minute=candidate.minute_index, status=EventStatus.APPLIED
            )
        )
        candidate.validate()
        self.world.__dict__.clear()
        self.world.__dict__.update(candidate.__dict__)
        receipt = CommandReceipt(
            command_id=f"command-{sequence}",
            branch_id=self.branch_id,
            minute=self.world.minute_index,
            event_id=event_id,
            state_hash=hashlib.sha256(self.world.canonical_json().encode("utf-8")).hexdigest(),
        )
        self.command_log.append({**asdict(receipt), "command": command.to_dict()})
        return receipt

    def apply_effect_program(self, program: EffectProgram) -> ScheduledEffectProgram:
        """Atomically schedule one whole validated EffectProgram on this branch."""
        if not isinstance(program, EffectProgram):
            raise BranchError("branch EffectProgram must use the versioned EffectProgram contract")
        try:
            receipt = schedule_effect_program(self.world, program)
        except ValueError as exc:
            raise BranchError(f"EffectProgram could not be scheduled: {exc}") from exc
        self.command_log.append(
            {
                "command": "effect_program",
                "program_id": receipt.program_id,
                "program_hash": receipt.program_hash,
                "event_ids": list(receipt.event_ids),
                "minute": self.world.minute_index,
            }
        )
        return receipt


def _apply_to_world(world: WorldState, command: BranchCommand) -> None:
    if isinstance(command, AddStaffCommand):
        if command.staff_id in world.staff:
            raise BranchError(f"staff {command.staff_id!r} already exists")
        if command.zone_id not in world.zones:
            raise BranchError(f"unknown staff zone {command.zone_id!r}")
        world.staff[command.staff_id] = StaffState(
            id=command.staff_id, zone_id=command.zone_id, skills=command.skills
        )
        from sheltersim.domain.staff_dynamics import add_staff
        from sheltersim.domain.staff_operations import add_staff_operations_agent
        from sheltersim.domain.movement import staff_position_snapshots
        add_staff(world, command.staff_id)
        add_staff_operations_agent(world, command.staff_id)
        if "movement_v1" in world.model_state:
            staff_position_snapshots(world)
        operational = world.model_state.get("operational_events_v1")
        if operational is not None:
            operational["staff_status"][command.staff_id] = {
                "available": True,
                "unavailability_event_id": None,
                "until_minute": None,
                "restore_event_id": None,
            }
    elif isinstance(command, AddToiletCommand):
        if command.facility_id in world.facilities:
            raise BranchError(f"facility {command.facility_id!r} already exists")
        if command.zone_id not in world.zones:
            raise BranchError(f"unknown facility zone {command.zone_id!r}")
        world.facilities[command.facility_id] = FacilityState(
            id=command.facility_id,
            kind="toilet",
            zone_id=command.zone_id,
            capacity=command.capacity,
            working_units=(
                command.capacity if command.working_units is None else command.working_units
            ),
        )
    elif isinstance(command, SetZoneEdgeCommand):
        edge = world.zone_edges.get(command.edge_id)
        if edge is None:
            raise BranchError(f"unknown zone edge {command.edge_id!r}")
        edge.travel_minutes = command.travel_minutes
        edge.closed = command.closed
        config = world.model_configs.setdefault(
            "layout_routing",
            {
                "schema_version": 1,
                "source_type": "assumption",
                "source_id": "ASM-A05-ROUTE-EDIT-V1",
                "max_agents_in_transit_by_edge": {},
            },
        )
        capacities = config["max_agents_in_transit_by_edge"]
        if command.max_agents_in_transit is None:
            capacities.pop(command.edge_id, None)
        else:
            capacities[command.edge_id] = command.max_agents_in_transit
        _validate_facility_reachability(world)
    elif isinstance(command, RelocateFacilityCommand):
        facility = world.facilities.get(command.facility_id)
        if facility is None:
            raise BranchError(f"unknown facility {command.facility_id!r}")
        if command.zone_id not in world.zones:
            raise BranchError(f"unknown facility zone {command.zone_id!r}")
        if any(
            (
                facility.active_agent_ids,
                facility.queue_agent_ids,
                facility.resource_active_agent_ids,
                facility.resource_queue_agent_ids,
            )
        ) or any(task.resource_id == facility.id for task in world.active_tasks.values()):
            raise BranchError("cannot relocate a facility with active users, queues, or tasks")
        facility.zone_id = command.zone_id
        _validate_facility_reachability(world)
    else:
        facility = world.facilities.get(command.facility_id)
        if facility is None:
            raise BranchError(f"unknown facility {command.facility_id!r}")
        modifier_is_pending = has_pending_facility_modifier(world, command.facility_id)
        if has_active_facility_modifier(world, command.facility_id) or modifier_is_pending:
            raise BranchError(
                "cannot directly change facility availability while "
                "an EffectProgram modifier is active"
            )
        if command.working_units > facility.capacity:
            raise BranchError("working_units cannot exceed facility capacity")
        if command.working_units < len(facility.active_agent_ids):
            raise BranchError("cannot reduce working units below active occupancy")
        facility.working_units = command.working_units


def _validate_facility_reachability(world: WorldState) -> None:
    route_config = world.model_configs.get("layout_routing", {})
    capacities = route_config.get("max_agents_in_transit_by_edge", {})
    router = SpaceRouter(
        ZoneGraph.from_states(world.zones, world.zone_edges, edge_capacities=capacities)
    )
    for facility in sorted(world.facilities.values(), key=lambda item: item.id):
        for zone_id in sorted(world.zones):
            if router.route(zone_id, facility.zone_id) is None:
                raise BranchError(
                    f"facility {facility.id!r} would be unreachable from zone {zone_id!r}"
                )


def _validate_identifier(name: str, value: str) -> str:
    if not isinstance(value, str) or not _BRANCH_ID.fullmatch(value):
        raise BranchError(f"{name} must be a stable identifier")
    return value


def _pairing_key(scenario_id: str, seed: int) -> str:
    payload = f"sheltersim:paired-rng:v1:{scenario_id}:{seed}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
