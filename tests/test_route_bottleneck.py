"""Synthetic layout validation and branchable route bottleneck behavior."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.layouts import LayoutSpec
from sheltersim.domain.zones import ZoneConnection, ZoneGraph, ZoneNode
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.experiments.branch import BranchError, RelocateFacilityCommand, _apply_to_world
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.policies.space import SpaceRouter
from sheltersim.web.demo import DemoError, DemoService

ROOT = Path(__file__).parents[1]
SCENARIO_PATH = ROOT / "fixtures/scenarios/h00_small_replay.json"
LAYOUT_PATH = ROOT / "fixtures/layouts/gym_synthetic.json"


def _scenario(duration_minutes: int = 30) -> ScenarioSpec:
    document = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = []
    return ScenarioSpec.from_json(json.dumps(document))


def _route_command(
    *,
    edge_id: str = "corridor_toilet",
    travel_minutes: int = 8,
    max_agents_in_transit: int | None = 1,
) -> dict[str, object]:
    return {
        "kind": "set_zone_edge",
        "edge_id": edge_id,
        "travel_minutes": travel_minutes,
        "closed": False,
        "max_agents_in_transit": max_agents_in_transit,
    }


def test_synthetic_layout_maps_scenario_zones_edges_and_facilities_one_to_one() -> None:
    layout = LayoutSpec.from_mapping(json.loads(LAYOUT_PATH.read_text(encoding="utf-8")))
    scenario = _scenario()

    layout.validate_scenario(scenario)

    assert {zone.zone_id for zone in layout.zones if zone.modelled} == {
        zone.id for zone in scenario.zones
    }
    assert {edge.edge_id for edge in layout.route_edges} == {
        edge.id for edge in scenario.zone_edges
    }
    assert all(
        facility.display_zone_id in {zone.zone_id for zone in layout.zones}
        for facility in layout.facilities
    )
    assert "合成" in layout.note


def test_layout_rejects_missing_graph_edges_and_out_of_canvas_polygons() -> None:
    document = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))
    document["route_edges"].pop()
    layout = LayoutSpec.from_mapping(document)
    with pytest.raises(ValueError, match="route edges"):
        layout.validate_scenario(_scenario())

    document = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))
    document["zones"][0]["polygon"][0] = [-1, 62]
    with pytest.raises(ValueError, match="outside the canvas"):
        LayoutSpec.from_mapping(document)


def test_visual_layout_edit_is_not_part_of_scenario_hash() -> None:
    scenario = _scenario()
    before = hashlib.sha256(scenario.canonical_json().encode()).hexdigest()
    layout = LayoutSpec.from_mapping(json.loads(LAYOUT_PATH.read_text(encoding="utf-8")))
    moved_zone = layout.zones[0].model_copy(
        update={"polygon": tuple((x + 10, y + 10) for x, y in layout.zones[0].polygon)}
    )
    moved_layout = layout.model_copy(update={"zones": (moved_zone, *layout.zones[1:])})
    after = hashlib.sha256(scenario.canonical_json().encode()).hexdigest()

    assert moved_layout.zones[0].polygon != layout.zones[0].polygon
    assert before == after


def test_route_policy_uses_an_open_alternative_when_an_edge_is_at_capacity() -> None:
    zones = {zone_id: ZoneNode(zone_id, zone_id) for zone_id in ("start", "hall", "desk", "end")}
    graph = ZoneGraph(
        zones=zones,
        edges=(
            ZoneConnection("start-hall", "start", "hall", 1, max_agents_in_transit=1),
            ZoneConnection("hall-end", "hall", "end", 1),
            ZoneConnection("start-desk", "start", "desk", 4),
            ZoneConnection("desk-end", "desk", "end", 4),
        ),
    )
    router = SpaceRouter(graph)

    route = router.route("start", "end", edge_loads={"start-hall": 1})

    assert route is not None
    assert route.edge_ids == ("start-desk", "desk-end")
    assert route.travel_minutes == 8


def test_capacity_block_is_recorded_with_route_and_assumption_provenance(tmp_path: Path) -> None:
    scenario = _scenario()
    world = WorldState.from_scenario(scenario)
    world.model_configs["layout_routing"] = {
        "schema_version": 1,
        "source_type": "assumption",
        "source_id": "ASM-A05-ROUTE-EDIT-V1",
        "max_agents_in_transit_by_edge": {"corridor_toilet": 1},
    }
    world.validate()
    active_travel = ScheduledEvent(
        event_id="active-route-test",
        kind=EventKind.COMPLETION,
        due_minute=5,
        priority=0,
        sequence=100,
        event_type="agent_travel_complete",
        payload={"route_edge_ids": ["sleeping_corridor", "corridor_toilet"]},
        source_id="active-route-test",
    )
    world.event_queue.schedule(active_travel)
    controller = OfflineSimulationController(
        world,
        RunArtifactWriter(scenario, tmp_path / "run"),
    )

    route = controller._route("sleeping", "toilet", "A001")

    assert route is None
    wait_record = world.event_log.records[-1]
    assert wait_record.event.event_type == "zone_edge_capacity_wait"
    assert wait_record.event.payload["edge_id"] == "corridor_toilet"
    assert wait_record.event.provenance["source_id"] == "ASM-A05-ROUTE-EDIT-V1"


def test_travel_time_only_route_edit_keeps_route_assumption_provenance(tmp_path: Path) -> None:
    scenario = _scenario()
    demo = DemoService(scenario)
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    result = demo.dispatch(
        "layout-branch",
        {
            "source_branch_id": "baseline",
            "idempotency_key": "route-time-provenance",
            "commands": [
                {
                    "kind": "set_zone_edge",
                    "edge_id": "corridor_toilet",
                    "travel_minutes": 8,
                    "closed": False,
                    "max_agents_in_transit": None,
                }
            ],
        },
    )
    world = demo.branches[result["branch_id"]].runtime.world
    assert world.model_configs["layout_routing"]["max_agents_in_transit_by_edge"] == {}
    controller = OfflineSimulationController(
        world,
        RunArtifactWriter(scenario, tmp_path / "route-provenance"),
    )
    route = controller._route("sleeping", "toilet", "A001")
    assert route is not None

    assert controller._schedule_travel(
        agent_id="A001",
        route=route,
        purpose="toilet_request",
        facility_id="toilet_1",
        minute=0,
    )

    travel_event = next(
        item for item in world.event_queue.pending() if item.event_type == "agent_travel_complete"
    )
    assert travel_event.provenance["source_id"] == "ASM-A05-ROUTE-EDIT-V1"


def test_comparison_reports_movement_and_capacity_wait_kpis_with_event_evidence() -> None:
    demo = DemoService(_scenario())
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    result = demo.dispatch(
        "layout-branch",
        {
            "source_branch_id": "baseline",
            "idempotency_key": "route-kpi-test",
            "commands": [_route_command()],
        },
    )
    baseline = demo.branches["baseline"]
    candidate = demo.branches[result["branch_id"]]
    world = candidate.runtime.world
    travel_sequence = world.allocate_event_sequence()
    travel_event = ScheduledEvent(
        event_id="route-kpi-travel",
        kind=EventKind.COMPLETION,
        due_minute=0,
        priority=0,
        sequence=travel_sequence,
        event_type="agent_travel_complete",
        payload={
            "agent_id": "A001",
            "travel_minutes": 8,
            "route_edge_ids": ["corridor_toilet"],
        },
        source_id="route-kpi-travel",
        provenance={"source_type": "assumption", "source_id": "ASM-A05-ROUTE-EDIT-V1"},
    )
    world.event_log.append(
        EventLogRecord(
            event=travel_event,
            realized_minute=0,
            status=EventStatus.APPLIED,
        )
    )
    wait_sequence = world.allocate_event_sequence()
    wait_event = ScheduledEvent(
        event_id="route-kpi-capacity-wait",
        kind=EventKind.ENDOGENOUS,
        due_minute=0,
        priority=0,
        sequence=wait_sequence,
        event_type="zone_edge_capacity_wait",
        payload={"agent_id": "A002", "edge_id": "corridor_toilet"},
        source_id="ASM-A05-ROUTE-EDIT-V1",
        provenance={"source_type": "assumption", "source_id": "ASM-A05-ROUTE-EDIT-V1"},
    )
    world.event_log.append(
        EventLogRecord(
            event=wait_event,
            realized_minute=0,
            status=EventStatus.SUPPRESSED,
            detail="route edge capacity is full",
        )
    )
    baseline.status = candidate.status = "completed"

    report = demo._comparison_payload("baseline", baseline, result["branch_id"], candidate)

    assert report["routing_metrics"]["travel_minutes_total"] == {
        "baseline": 0,
        "candidate": 8,
        "delta": 8,
    }
    assert report["routing_metrics"]["capacity_wait_observation_count"]["delta"] == 1
    evidence = report["routing_evidence"]["candidate"]
    assert evidence["completed_travel_events"][0]["event_id"] == "route-kpi-travel"
    assert evidence["completed_travel_events"][0]["source_id"] == "ASM-A05-ROUTE-EDIT-V1"
    assert evidence["capacity_wait_events"][0]["event_id"] == "route-kpi-capacity-wait"
    assert evidence["capacity_wait_events"][0]["edge_id"] == "corridor_toilet"


def test_route_edit_branches_are_atomic_reproducible_and_leave_source_unchanged() -> None:
    demo = DemoService(_scenario())
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    before_hash = demo.branches["baseline"].runtime.world.canonical_json()
    payload = {
        "source_branch_id": "baseline",
        "idempotency_key": "route-edit-1",
        "commands": [_route_command()],
    }

    result = demo.dispatch("layout-branch", payload)
    replay = demo.dispatch("layout-branch", payload)
    child = demo.branches[result["branch_id"]].runtime

    assert result["parent_branch_id"] == "baseline"
    assert result["branch_id"] == replay["branch_id"]
    assert replay["idempotent_replay"] is True
    assert demo.branches["baseline"].runtime.world.canonical_json() == before_hash
    assert child.world.zone_edges["corridor_toilet"].travel_minutes == 8
    assert child.world.model_configs["layout_routing"]["max_agents_in_transit_by_edge"] == {
        "corridor_toilet": 1
    }


def test_invalid_route_edit_set_creates_no_partial_branch() -> None:
    demo = DemoService(_scenario())
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    before_hash = demo.branches["baseline"].runtime.world.canonical_json()

    with pytest.raises(DemoError, match="unknown zone edge"):
        demo.dispatch(
            "layout-branch",
            {
                "source_branch_id": "baseline",
                "idempotency_key": "route-edit-invalid",
                "commands": [_route_command(), _route_command(edge_id="missing-edge")],
            },
        )

    assert list(demo.branches) == ["baseline"]
    assert demo.branches["baseline"].runtime.world.canonical_json() == before_hash


def test_route_edit_that_disconnects_a_facility_is_rejected_atomically() -> None:
    demo = DemoService(_scenario())
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    before_hash = demo.branches["baseline"].runtime.world.canonical_json()

    with pytest.raises(DemoError, match="would be unreachable"):
        demo.dispatch(
            "layout-branch",
            {
                "source_branch_id": "baseline",
                "idempotency_key": "route-edit-disconnect",
                "commands": [
                    {
                        "kind": "set_zone_edge",
                        "edge_id": "corridor_toilet",
                        "travel_minutes": 2,
                        "closed": True,
                        "max_agents_in_transit": None,
                    }
                ],
            },
        )

    assert list(demo.branches) == ["baseline"]
    assert demo.branches["baseline"].runtime.world.canonical_json() == before_hash


def test_facility_relocation_is_a_typed_branch_intervention() -> None:
    demo = DemoService(_scenario())
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    source = demo.branches["baseline"].runtime
    before_hash = source.world.canonical_json()

    result = demo.dispatch(
        "layout-branch",
        {
            "source_branch_id": "baseline",
            "idempotency_key": "facility-relocation-1",
            "commands": [
                {"kind": "relocate_facility", "facility_id": "toilet_1", "zone_id": "corridor"}
            ],
        },
    )

    child = demo.branches[result["branch_id"]].runtime
    assert source.world.facilities["toilet_1"].zone_id == "toilet"
    assert child.world.facilities["toilet_1"].zone_id == "corridor"
    assert child.command_log[-1]["command"]["kind"] == "relocate_facility"
    assert child.world.canonical_json() != before_hash


def test_facility_relocation_rejects_unknown_zone_without_partial_change() -> None:
    demo = DemoService(_scenario())
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    before_hash = demo.branches["baseline"].runtime.world.canonical_json()

    with pytest.raises(DemoError, match="unknown facility zone"):
        demo.dispatch(
            "layout-branch",
            {
                "source_branch_id": "baseline",
                "idempotency_key": "facility-relocation-invalid",
                "commands": [
                    {"kind": "relocate_facility", "facility_id": "toilet_1", "zone_id": "missing"}
                ],
            },
        )

    assert list(demo.branches) == ["baseline"]
    assert demo.branches["baseline"].runtime.world.canonical_json() == before_hash


def test_relocate_facility_command_rejects_active_facility_use() -> None:
    world = WorldState.from_scenario(_scenario())
    world.facilities["toilet_1"].queue_agent_ids.append("A001")

    with pytest.raises(BranchError, match="active users, queues, or tasks"):
        _apply_to_world(world, RelocateFacilityCommand("toilet_1", "corridor"))
