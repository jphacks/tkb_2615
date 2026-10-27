"""Zone routing, explicit travel time, closure, capacity, provenance and policy injection tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim.core.world import WorldState
from sheltersim.domain.zones import ZoneConnection, ZoneGraph, ZoneNode
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.policies.space import SpaceRouter, ZoneRoute
from sheltersim.web.demo import DemoService


def fixture_graph() -> ZoneGraph:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    return ZoneGraph.from_states(world.zones, world.zone_edges)


def test_shortest_travel_time_route_uses_explicit_edge_times() -> None:
    zones = {
        zone_id: ZoneNode(zone_id, zone_id)
        for zone_id in ("sleeping", "corridor", "toilet", "desk")
    }
    graph = ZoneGraph(
        zones=zones,
        edges=(
            ZoneConnection("sleeping_corridor", "sleeping", "corridor", 4),
            ZoneConnection("corridor_toilet", "corridor", "toilet", 7),
            ZoneConnection("sleeping_toilet", "sleeping", "toilet", 14),
            ZoneConnection("sleeping_desk", "sleeping", "desk", 4),
            ZoneConnection("desk_toilet", "desk", "toilet", 7),
        ),
    )
    router = SpaceRouter(graph)

    route = router.route("sleeping", "toilet", agent_id="A001")

    assert route is not None
    assert route.zone_ids == ("sleeping", "corridor", "toilet")
    assert route.edge_ids == ("sleeping_corridor", "corridor_toilet")
    assert route.travel_minutes == 11
    assert router.travel_minutes("sleeping", "toilet") == 11


def test_unconnected_zone_is_unreachable_and_unknown_zone_is_explicit_error() -> None:
    graph = fixture_graph()
    graph_with_island = ZoneGraph(
        zones={**graph.zones, "island": ZoneNode("island", "Island")},
        edges=graph.edges,
    )
    router = SpaceRouter(graph_with_island)

    assert router.route("sleeping", "toilet") is not None
    assert router.route("sleeping", "island") is None
    with pytest.raises(ValueError, match="unknown origin zone"):
        router.route("unconnected", "toilet")
    assert router.travel_minutes("sleeping", "island") is None


def test_closed_edge_changes_reachability() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    open_route = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges)).route(
        "sleeping", "toilet"
    )
    world.zone_edges["corridor_toilet"].closed = True
    closed_route = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges)).route(
        "sleeping", "toilet"
    )

    assert open_route is not None
    assert open_route.travel_minutes == 2
    assert closed_route is None


def test_display_coordinates_do_not_change_physical_route() -> None:
    graph = fixture_graph()
    moved_nodes = {
        key: ZoneNode(
            id=node.id,
            name=node.name,
            capacity=node.capacity,
            display_x=1000.0,
            display_y=-1000.0,
        )
        for key, node in graph.zones.items()
    }
    relocated_graph = ZoneGraph(zones=moved_nodes, edges=graph.edges)

    original = SpaceRouter(graph).route("sleeping", "toilet")
    relocated = SpaceRouter(relocated_graph).route("sleeping", "toilet")

    assert relocated == original


def test_alternative_space_policy_can_be_injected_without_core_changes() -> None:
    graph = fixture_graph()

    class FixedPolicy:
        def __init__(self) -> None:
            self.seen_agent: str | None = None

        def route(self, graph, origin_zone_id, destination_zone_id, agent_id=None):
            self.seen_agent = agent_id
            return ZoneRoute(
                origin_zone_id=origin_zone_id,
                destination_zone_id=destination_zone_id,
                zone_ids=(origin_zone_id, destination_zone_id),
                edge_ids=("custom-route",),
                travel_minutes=9,
            )

    policy = FixedPolicy()
    router = SpaceRouter(graph, policy=policy)

    assert router.travel_minutes("sleeping", "toilet", agent_id="A042") == 9
    assert policy.seen_agent == "A042"


def test_zone_graph_is_a_validated_read_only_snapshot() -> None:
    graph = fixture_graph()

    with pytest.raises(TypeError):
        graph.zones["other"] = ZoneNode("other", "Other")  # type: ignore[index]
    with pytest.raises(ValueError, match="unknown zone"):
        ZoneGraph(
            zones={"sleeping": ZoneNode("sleeping", "Sleeping")},
            edges=(ZoneConnection("broken", "sleeping", "missing", 1),),
        )


def test_equal_duration_routes_use_stable_edge_id_tiebreaking() -> None:
    zones = {key: ZoneNode(key, key) for key in ("a", "b", "c", "d")}
    edges = (
        ZoneConnection("z_a_d", "a", "d", 2),
        ZoneConnection("z_d_c", "d", "c", 2),
        ZoneConnection("a_a_b", "a", "b", 2),
        ZoneConnection("b_b_c", "b", "c", 2),
    )
    route_a = SpaceRouter(ZoneGraph(zones, edges)).route("a", "c")
    route_b = SpaceRouter(ZoneGraph(zones, tuple(reversed(edges)))).route("a", "c")

    assert route_a == route_b
    assert route_a is not None
    assert route_a.edge_ids == ("a_a_b", "b_b_c")


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


def test_capacity_block_is_recorded_with_route_and_assumption_provenance(
    tmp_path: Path, small_replay_scenario
) -> None:
    scenario = small_replay_scenario()
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


def test_travel_time_only_route_edit_keeps_route_assumption_provenance(
    tmp_path: Path, small_replay_scenario
) -> None:
    scenario = small_replay_scenario()
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
