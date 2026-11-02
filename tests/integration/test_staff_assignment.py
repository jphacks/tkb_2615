"""The staff's assignment in Rust: a task, a relief, a route's wait and a preemption.

native/src/rules/staff.rs and travel.rs against their Python rules on twin worlds, on what the gate
reaches rarely or never (docs/rust-port/lanes/l2-staff.md). No Run reaches `_preempt_background_staff`,
so a Run here does, through the driver and through the phases.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim import native
from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.domain.models import SupportRequestState, SupportRequestStatus
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.domain.staff import StaffService
from sheltersim.domain.staff_operations import (
    STAFF_OPERATIONS_STATE_KEY,
    assign_staff_operations,
    schedule_staff_operation_relief,
)
from sheltersim.domain.movement import shared_passage_edge_loads
from sheltersim.domain.zones import ZoneGraph
from sheltersim.experiments import metrics
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.experiments.phases.propose import ProposePhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.space import SpaceRouter

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

KEY = STAFF_OPERATIONS_STATE_KEY


def _document(
    *,
    staff: list[dict[str, Any]] | None = None,
    duration: int = 200,
    configs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document = ScenarioSpec.load(SCENARIO_100_AGENTS).model_dump(mode="json")
    document["duration_minutes"] = duration
    document["scheduled_events"] = []
    if staff is not None:
        document["staff"] = staff
    document["model_configs"]["staff_operations"] = {}
    document["model_configs"]["staff_dynamics"] = {}
    document["model_configs"]["shared_operations"] = {
        "schema_version": 1,
        "policy_version": "shared-operations-v1",
    }
    document["model_configs"].update(configs or {})
    return document


def _world(**options: Any) -> WorldState:
    world = WorldState.from_scenario(ScenarioSpec.model_validate(_document(**options)))
    initialize_simulator_extensions(world)
    return world


def _routing(capacities: dict[str, int]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_type": "assumption",
        "source_id": "ASM-A05-ROUTE-EDIT-V1",
        "max_agents_in_transit_by_edge": capacities,
    }


def _member(name: str, zone: str, *skills: str) -> dict[str, Any]:
    return {"id": name, "zone_id": zone, "skills": list(skills)}


def _state(world: WorldState) -> dict[str, Any]:
    return {
        "world": json.loads(world._canonical_text(world._canonical_data())),
        "records": [raw.decode() for raw in world.event_log.items_bytes(0, len(world.event_log))],
        "pending": [event.to_dict() for event in world.event_queue.pending()],
        "counters": dict(world.id_counters),
    }


def _router(world: WorldState) -> SpaceRouter:
    return SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))


def _zones(world: WorldState) -> list[str]:
    return sorted(world.zones)


def _both_assign(first: WorldState, second: WorldState, minute: int, **options: Any) -> Any:
    router = options.pop("router", None)
    answer = native.rules("staff").assign_operations(first, minute, router or _router(first))
    expected = assign_staff_operations.__wrapped__(second, minute, router or _router(second))
    assert answer == expected
    assert json.dumps(_state(first)) == json.dumps(_state(second))
    assert first._core.mirror(KEY).verify()
    return answer


def _spread(world: WorldState, zone: str, stress: float) -> None:
    for agent in world.agents.values():
        agent.zone_id = zone
        agent.stress = stress


# ---- assign_staff_operations ----


def test_a_format_g_is_python_s() -> None:
    kernel = native.rules("staff").format_g
    generator = random.Random(20261102)
    values = [0.0, -0.0, 1.0, 0.85, 0.45, 0.6, 1e-5, 0.0001, 0.00001234, 123456.0, 1234567.0]
    values += [1e6, 999999.5, 1e-300, 0.1 + 0.2, 1.5, 2.5, 0.000012345678, 100000.0, 12.5]
    values += [generator.uniform(0, 1) for _ in range(2000)]
    values += [10 ** generator.uniform(-8, 9) for _ in range(2000)]
    values += [round(generator.uniform(0, 1), generator.randrange(1, 7)) for _ in range(500)]
    for value in values:
        assert kernel(value) == format(value, "g")


@pytest.mark.parametrize("policy", [None, "staff-urgency-first-v1", "staff-travel-first-v1"])
def test_a_worker_takes_the_task_the_python_rule_ranks_first(policy: str | None) -> None:
    configs: dict[str, Any] = {}
    if policy is not None:
        configs["policy_registry"] = {"schema_version": 2, "staff_operations": policy}

    def make() -> WorldState:
        world = _world(
            staff=[
                _member("S01", "toilet", "cleaning", "patrol", "maintenance"),
                _member("S02", "sleeping", "cleaning", "support"),
                _member("S03", "toilet", "repair"),
            ],
            configs=configs,
        )
        for zone, record in world.model_state[KEY]["zones"].items():
            record["sanitation"] = 0.5 if zone == "toilet" else 0.7
        _spread(world, "sleeping", 0.8)
        facility = sorted(world.facilities)[0]
        world.facilities[facility].working_units = world.facilities[facility].capacity - 1
        return world

    first, second = make(), make()
    answer = _both_assign(first, second, 0)
    assert len(answer) == 2
    kinds = {record["task_kind"] for record in first.model_state[KEY]["staff"].values()}
    assert len(kinds) >= 2


def test_a_zone_s_threshold_names_the_reason_as_python_formats_it() -> None:
    thresholds = {"toilet": {"cleaning_sanitation_below": 1}}

    def make() -> WorldState:
        world = _world(
            staff=[_member("S01", "toilet", "cleaning")],
            configs={"staff_operations": {"thresholds_by_zone": thresholds}},
        )
        world.model_state[KEY]["zones"]["toilet"]["sanitation"] = 0.99
        return world

    first, second = make(), make()
    assert _both_assign(first, second, 0)
    assert first.model_state[KEY]["staff"]["S01"]["reason"] == "sanitation_index_below_1"


def test_the_rotation_reads_the_history_and_the_fatigue() -> None:
    def make(fatigue: float) -> WorldState:
        world = _world(staff=[_member(f"S{n}", "toilet", "patrol") for n in range(3)])
        _spread(world, "toilet", 0.9)
        state = world.model_state["staff_dynamics_v1"]["staff"]
        state["S0"]["fatigue"] = fatigue
        state["S1"]["fatigue"] = 0.0005
        return world

    # 1000 * 0.0005 is half way: round() goes to even, and the ties go by ID.
    for fatigue in (0.0005, 0.0015, 0.0025, 0.001):
        first, second = make(fatigue), make(fatigue)
        answer = _both_assign(first, second, 0)
        assert len(answer) == 1


def test_a_patrol_s_urgency_is_half_the_mean_stress_against_another_task() -> None:
    """Either side of the rank: a 1% change of the factor swaps them."""

    def make(sanitation: float) -> WorldState:
        world = _world(staff=[_member("S01", "toilet", "cleaning", "patrol")])
        _spread(world, "toilet", 0.8)
        for zone, record in world.model_state[KEY]["zones"].items():
            record["sanitation"] = sanitation if zone == "toilet" else 1.0
        return world

    for sanitation, kind in ((0.598, "cleaning"), (0.602, "patrol")):
        first, second = make(sanitation), make(sanitation)
        assert _both_assign(first, second, 0)
        assert first.model_state[KEY]["staff"]["S01"]["task_kind"] == kind


def test_the_rotation_counts_recent_work_from_four_hours_back() -> None:
    def make() -> WorldState:
        world = _world(staff=[_member(f"S{n}", "toilet", "patrol") for n in range(2)], duration=400)
        _spread(world, "toilet", 0.9)
        world.minute_index = 300
        entry = {
            "task_id": "old",
            "task_kind": "patrol",
            "target_id": None,
            "target_zone_id": "toilet",
            "reason": "r",
            "status": "completed",
            "start_minute": 0,
            "arrival_minute": 0,
            "travel_minutes": 0,
            "causation_event_ids": [],
        }
        history = world.model_state[KEY]["history"]
        history.append({**entry, "staff_id": "S0", "complete_minute": 60})
        history.append({**entry, "staff_id": "S1", "complete_minute": 59})
        return world

    first, second = make(), make()
    assert _both_assign(first, second, 300)
    state = first.model_state[KEY]["staff"]
    assert state["S1"]["phase"] != "idle" and state["S0"]["phase"] == "idle"


def test_a_fatigue_rounds_at_a_thousandth_in_the_rotation() -> None:
    def make() -> WorldState:
        world = _world(staff=[_member(f"S{n}", "toilet", "patrol") for n in range(2)])
        _spread(world, "toilet", 0.9)
        state = world.model_state["staff_dynamics_v1"]["staff"]
        state["S0"]["fatigue"] = 0.0504
        state["S1"]["fatigue"] = 0.0498
        return world

    first, second = make(), make()
    assert _both_assign(first, second, 0)
    # Both round to 50, so the ID decides; 1010 * fatigue would give 51 and 50.
    assert first.model_state[KEY]["staff"]["S0"]["phase"] != "idle"


def test_the_travel_first_formula_takes_the_nearer_task() -> None:
    def make(policy: str) -> WorldState:
        world = _world(
            staff=[_member("S01", "toilet", "cleaning")],
            configs={"policy_registry": {"schema_version": 2, "staff_operations": policy}},
        )
        for zone, record in world.model_state[KEY]["zones"].items():
            record["sanitation"] = 0.8 if zone == "toilet" else 0.2
        return world

    near = make("staff-travel-first-v1"), make("staff-travel-first-v1")
    assert _both_assign(*near, 0)
    assert near[0].model_state[KEY]["staff"]["S01"]["target_zone_id"] == "toilet"
    urgent = make("staff-urgency-first-v1"), make("staff-urgency-first-v1")
    assert _both_assign(*urgent, 0)
    assert urgent[0].model_state[KEY]["staff"]["S01"]["target_zone_id"] != "toilet"


def test_equal_ranks_go_by_the_kind_zone_and_target_as_python_compares_them() -> None:
    def make() -> WorldState:
        world = _world(staff=[_member("S01", "toilet", "cleaning")])
        for record in world.model_state[KEY]["zones"].values():
            record["sanitation"] = 0.5
        world.zone_edges[next(iter(world.zone_edges))].closed = False
        return world

    first, second = make(), make()
    assert _both_assign(first, second, 0)


def test_a_full_passage_keeps_a_worker_from_a_task() -> None:
    capacities = {"layout_routing": _routing({})}

    def make() -> tuple[WorldState, SpaceRouter]:
        world = _world(
            staff=[_member("S01", "toilet", "patrol"), _member("S02", "toilet", "patrol")],
            configs=capacities,
        )
        _spread(world, "sleeping", 0.9)
        graph = ZoneGraph.from_states(
            world.zones, world.zone_edges, edge_capacities={edge: 1 for edge in world.zone_edges}
        )
        return world, SpaceRouter(graph)

    first, first_router = make()
    second, second_router = make()
    answer = native.rules("staff").assign_operations(first, 0, first_router)
    expected = assign_staff_operations.__wrapped__(second, 0, second_router)
    assert answer == expected and len(answer) == 1
    assert json.dumps(_state(first)) == json.dumps(_state(second))


def test_an_unknown_formula_and_a_corrupt_value_stay_the_python_rule_s() -> None:
    world = _world(staff=[_member("S01", "toilet", "patrol")])
    _spread(world, "toilet", 0.9)
    before = json.dumps(_state(world))
    world.model_state["staff_dynamics_v1"]["staff"]["S01"]["fatigue"] = float("inf")
    with pytest.raises(native.Unsupported):
        native.rules("staff").assign_operations(world, 0, _router(world))
    with pytest.raises(OverflowError):
        assign_staff_operations.__wrapped__(world, 0, _router(world))
    world.model_state["staff_dynamics_v1"]["staff"]["S01"]["fatigue"] = 0.0
    assert json.loads(before)["counters"] == dict(world.id_counters)
    world.model_configs["policy_registry"] = {"staff_operations": "a-later-formula"}
    with pytest.raises(native.Unsupported):
        native.rules("staff").assign_operations(world, 0, _router(world))


def test_the_passage_loads_are_the_python_rule_s() -> None:
    """`shared_passage_edge_loads` through what `assign_staff_operations` starts on them."""

    def make() -> WorldState:
        world = _world(staff=[_member(f"S{n}", "toilet", "patrol") for n in range(3)])
        _spread(world, "sleeping", 0.9)
        return world

    first, second = make(), make()
    _both_assign(first, second, 0)
    assert shared_passage_edge_loads(first, 1) == shared_passage_edge_loads(second, 1)
    assert shared_passage_edge_loads(first, 1)
    for each in (first, second):
        each.minute_index = 1
    # The zones stay reserved, so a later minute starts nothing new on them and reads the loads.
    _both_assign(first, second, 1)


# ---- schedule_staff_operation_relief ----


def _on_a_task() -> WorldState:
    world = _world(
        duration=40,
        staff=[
            _member("S01", "toilet", "cleaning"),
            _member("S02", "toilet", "sanitation"),
            _member("S03", "toilet", "sanitation"),
            _member("S04", "sleeping", "cleaning"),
        ],
        configs={
            "staff_dynamics": {
                "shift_windows_by_staff": {
                    "S01": [{"start_minute": 0, "end_minute": 2}],
                    "S02": [{"start_minute": 2, "end_minute": 40}],
                    "S03": [{"start_minute": 2, "end_minute": 40}],
                    "S04": [{"start_minute": 2, "end_minute": 40}],
                }
            },
            "staff_operations": {
                "task_duration_minutes": {
                    "cleaning": 8,
                    "maintenance": 8,
                    "information_outreach": 8,
                    "intake": 8,
                    "patrol": 8,
                }
            },
        },
    )
    world.model_state[KEY]["zones"]["toilet"]["sanitation"] = 0.5
    assert assign_staff_operations(world, 0, _router(world))
    return world


def test_a_relief_goes_to_the_replacement_the_python_rule_picks() -> None:
    first, second = _on_a_task(), _on_a_task()
    first.minute_index = second.minute_index = 1
    answer = native.rules("staff").relief(first, 1)
    expected = schedule_staff_operation_relief.__wrapped__(second, 1)
    assert answer == expected and len(answer) == 1
    assert json.dumps(_state(first)) == json.dumps(_state(second))
    # The one scheduled is not asked for again, and neither is one at a later minute.
    assert native.rules("staff").relief(first, 1) == ()
    assert schedule_staff_operation_relief.__wrapped__(second, 1) == ()
    assert next(
        event.payload["replacement_staff_id"]
        for event in first.event_queue.pending()
        if event.event_type == "staff_operation_relief"
    ) in {"S02", "S03"}


def test_a_relief_goes_to_the_replacement_with_the_least_work() -> None:
    def make() -> WorldState:
        world = _on_a_task()
        world.minute_index = 1
        world.model_state["staff_dynamics_v1"]["staff"]["S02"]["fatigue"] = 0.5
        return world

    first, second = make(), make()
    answer = native.rules("staff").relief(first, 1)
    assert answer == schedule_staff_operation_relief.__wrapped__(second, 1)
    assert json.dumps(_state(first)) == json.dumps(_state(second))
    assert next(
        event.payload["replacement_staff_id"]
        for event in first.event_queue.pending()
        if event.event_type == "staff_operation_relief"
    ) == "S03"


def test_no_relief_for_a_worker_still_on_shift_or_too_near_the_end() -> None:
    world = _on_a_task()
    assert native.rules("staff").relief(world, 0) == ()
    assert schedule_staff_operation_relief.__wrapped__(world, 0) == ()
    world.model_state[KEY]["staff"]["S01"]["due_minute"] = 2
    world.minute_index = 1
    assert native.rules("staff").relief(world, 1) == ()
    assert schedule_staff_operation_relief.__wrapped__(world, 1) == ()


# ---- What the metrics read, with routes ----


def _both_snapshots(world: WorldState, minute: int) -> dict[str, Any]:
    active = sorted(
        (
            request
            for request in world.support_requests.values()
            if request.status in (SupportRequestStatus.PENDING, SupportRequestStatus.ASSIGNED)
        ),
        key=lambda request: request.id,
    )
    answer = native.rules("staff").wait_snapshot(world, active, minute)
    assert json.dumps(answer) == json.dumps(
        metrics._support_wait_snapshot.__wrapped__(world, active, minute)
    )
    return answer[0]


def _pending(world: WorldState, number: int, agent_id: str, minute: int = 0) -> SupportRequestState:
    request = SupportRequestState(
        id=f"support-{number}",
        agent_id=agent_id,
        requested_minute=minute,
        timeout_minute=minute + 90,
    )
    world.support_requests[request.id] = request
    return request


def _reasons(world: WorldState, minute: int = 0) -> list[list[str]]:
    return [detail["reason_codes"] for detail in _both_snapshots(world, minute).values()]


def test_the_reasons_a_free_worker_s_route_gives_are_the_python_rule_s() -> None:
    world = _world(staff=[_member("S01", "toilet", "support")], duration=200)
    people = sorted(world.agents)
    for agent in world.agents.values():
        agent.zone_id = "sleeping"
    _pending(world, 1, people[0])
    assert _reasons(world) == [["assignment_not_attempted_at_sampled_minute"]]
    world.metric_state["support_assignment_metrics_minute"] = 0.0
    assert _reasons(world) == [["assignment_failure_reason_not_recorded"]]
    request = world.support_requests["support-1"]
    request.last_assignment_attempt_minute = 0
    assert _reasons(world) == [["assignment_failure_reason_not_recorded"]]
    request.last_assignment_wait_reason_code = "route_unavailable_at_assignment"
    assert _reasons(world) == [["route_unavailable_at_assignment"]]


def test_a_route_past_the_end_of_the_run_is_its_reason() -> None:
    world = _world(staff=[_member("S01", "toilet", "support")], duration=3)
    for agent in world.agents.values():
        agent.zone_id = "sleeping"
    _pending(world, 1, sorted(world.agents)[0])
    assert _reasons(world) == [["insufficient_run_time"]]


def test_a_route_that_ends_exactly_at_the_run_s_end_is_too_late() -> None:
    plain = _world(staff=[_member("S01", "toilet", "support")])
    route = _router(plain).route("sleeping", "toilet")
    assert route is not None
    # Its minutes, and ten of service.
    last = route.travel_minutes + 10
    for duration, too_late in ((last, True), (last + 1, False)):
        world = _world(staff=[_member("S01", "toilet", "support")], duration=duration)
        for agent in world.agents.values():
            agent.zone_id = "sleeping"
        _pending(world, 1, sorted(world.agents)[0])
        assert ("insufficient_run_time" in _reasons(world)[0]) is too_late


def test_a_full_destination_counts_who_is_on_the_way() -> None:
    from sheltersim.domain.models import Activity

    world = _world(staff=[_member("S01", "toilet", "support")])
    people = sorted(world.agents)
    for agent in world.agents.values():
        agent.zone_id = "sleeping"
    world.agents[people[1]].zone_id = "toilet"
    world.agents[people[2]].activity = Activity.MOVING
    world.agents[people[2]].target_id = "toilet"
    _pending(world, 1, people[0])
    world.zones["toilet"].capacity = 2
    assert _reasons(world) == [["staff_destination_full"]]
    world.zones["toilet"].capacity = 3
    assert _reasons(world) != [["staff_destination_full"]]


def test_a_full_destination_and_a_full_passage_are_their_reasons() -> None:
    world = _world(staff=[_member("S01", "toilet", "support")])
    people = sorted(world.agents)
    for agent in world.agents.values():
        agent.zone_id = "sleeping"
    _pending(world, 1, people[0])
    world.zones["toilet"].capacity = 0
    assert _reasons(world) == [["staff_destination_full"]]
    world.zones["toilet"].capacity = None
    world.model_configs["layout_routing"] = _routing({edge: 1 for edge in world.zone_edges})
    state = world.model_state[KEY]["staff"]
    for edge in world.zone_edges.values():
        world.schedule_event(_trip(world, edge.id))
    assert state
    assert _reasons(world) == [["route_capacity_full"]]
    for edge in world.zone_edges.values():
        edge.closed = True
    assert _reasons(world) == [["no_route_to_qualified_staff"]]


def _trip(world: WorldState, edge_id: str) -> Any:
    from sheltersim.events.models import EventKind, ScheduledEvent

    sequence = world.allocate_event_sequence()
    return ScheduledEvent(
        event_id=f"agent-travel-complete-{sequence}",
        kind=EventKind.COMPLETION,
        due_minute=world.duration_minutes - 1,
        priority=0,
        sequence=sequence,
        event_type="agent_travel_complete",
        payload={"agent_id": "nobody", "task_id": "none", "route_edge_ids": [edge_id]},
        source_id="test",
        provenance={"source_type": "assumption", "source_id": "test"},
    )


def test_many_worlds_read_the_same_reasons_with_free_and_busy_workers() -> None:
    generator = random.Random(2615)
    seen: set[str] = set()
    for _ in range(120):
        world = _world(
            staff=[
                _member("S01", generator.choice(["toilet", "sleeping"]), "support"),
                _member("S02", generator.choice(["toilet", "sleeping"]), "medical"),
                _member("S03", generator.choice(["toilet", "sleeping"]), "support", "nursing"),
            ],
            duration=generator.choice([3, 5, 200]),
        )
        for agent in world.agents.values():
            agent.zone_id = generator.choice(["toilet", "sleeping"])
        for staff in world.staff.values():
            if generator.random() < 0.4:
                staff.active_task_id = "busy"
        people = sorted(world.agents)
        for number in range(4):
            request = _pending(world, number, people[number])
            if generator.random() < 0.3:
                request.last_assignment_attempt_minute = 0
                request.last_assignment_wait_reason_code = generator.choice(
                    [None, "", "insufficient_run_time"]
                )
        if generator.random() < 0.5:
            world.metric_state["support_assignment_metrics_minute"] = 0.0
        if generator.random() < 0.3:
            world.zones[generator.choice(_zones(world))].capacity = generator.choice([0, 1, 300])
        for reasons in _reasons(world):
            seen.update(reasons)
    assert {
        "staff_busy",
        "assignment_not_attempted_at_sampled_minute",
        "assignment_failure_reason_not_recorded",
        "insufficient_run_time",
        "staff_destination_full",
    } <= seen


# ---- assign_pending ----


def test_a_free_worker_or_a_priority_hands_the_step_to_assign_work() -> None:
    world = _world(staff=[_member("S01", "toilet", "support")])
    request = _pending(world, 1, sorted(world.agents)[0])
    service = StaffService(world)
    before = json.dumps(_state(world))
    kernel = native.rules("staff").assign_none
    assert kernel(service, 0, validate_state=False) is None
    assert kernel(service, 0) is None
    assert kernel(service, 1, validate_state=False) is None
    assert json.dumps(_state(world)) == before
    world.staff["S01"].active_task_id = "busy"
    world.model_state["llm"] = {"support_priorities": {request.id: 1}}
    assert kernel(service, 0, validate_state=False) is None
    world.model_state["llm"] = {"support_priorities": {}}
    assert kernel(service, 0, validate_state=False) == ()


def test_a_wrong_minute_is_the_python_rule_s_error() -> None:
    world = _world(staff=[_member("S01", "toilet", "support")])
    service = StaffService(world)
    with pytest.raises(ValueError, match="must match WorldState"):
        service.assign_pending(5)


# ---- _route with edge capacities ----


def _capacity_controller(tmp_path: Path, name: str, capacity: int = 1) -> OfflineSimulationController:
    document = _document(staff=[_member("S01", "toilet", "support")])
    edges = [edge["id"] for edge in document["zone_edges"]]
    document["model_configs"]["layout_routing"] = _routing({edge: capacity for edge in edges})
    scenario = ScenarioSpec.model_validate(document)
    world = WorldState.from_scenario(scenario)
    artifacts = RunArtifactWriter(scenario, tmp_path / name, trace_level=TraceLevel.OFF)
    artifacts.write_initial(world, execution_mode="offline")
    return OfflineSimulationController(world, artifacts)


def test_a_route_with_a_full_edge_records_its_wait_as_the_python_rule_does(tmp_path: Path) -> None:
    first = _capacity_controller(tmp_path, "first")
    second = _capacity_controller(tmp_path, "second")
    people = sorted(first.world.agents)
    route = native.rules("travel").route
    for controller in (first, second):
        for agent in controller.world.agents.values():
            agent.zone_id = "sleeping"
        for edge in controller.world.zone_edges:
            controller.world.schedule_event(_trip(controller.world, edge))
    answer = route(first, "sleeping", "toilet", people[0])
    expected = ProposePhase._route(second, "sleeping", "toilet", people[0])
    assert answer == expected is None
    answer = route(first, "sleeping", "sleeping", people[0])
    assert answer == ProposePhase._route(second, "sleeping", "sleeping", people[0])
    assert answer.edge_ids == ()
    assert json.dumps(_state(first.world)) == json.dumps(_state(second.world))
    assert [
        record.event.event_type
        for record in first.world.event_log.records
        if record.event.event_type == "zone_edge_capacity_wait"
    ] == ["zone_edge_capacity_wait"]


def test_a_route_with_room_on_its_edges_counts_the_trips_under_way(tmp_path: Path) -> None:
    first = _capacity_controller(tmp_path, "first", capacity=2)
    second = _capacity_controller(tmp_path, "second", capacity=2)
    people = sorted(first.world.agents)
    for controller in (first, second):
        for edge in controller.world.zone_edges:
            controller.world.schedule_event(_trip(controller.world, edge))
    answer = native.rules("travel").route(first, "sleeping", "toilet", people[0])
    assert answer == ProposePhase._route(second, "sleeping", "toilet", people[0])
    assert answer is not None and answer.edge_ids
    assert json.dumps(_state(first.world)) == json.dumps(_state(second.world))


def test_a_full_edge_in_a_request_records_the_wait_before_the_trip_s_sequence(
    tmp_path: Path,
) -> None:
    """`_select_toilet` goes through `_route`, in the plan of `apply_effect`."""

    def run(name: str, *, on_the_phases: bool) -> dict[str, Any]:
        controller = _capacity_controller(tmp_path, name)
        world = controller.world
        for agent in world.agents.values():
            agent.zone_id = "sleeping"
        for edge in world.zone_edges:
            world.schedule_event(_trip(world, edge))
        native.rules("tick").python_path_only(on_the_phases)
        try:
            engine = TickEngine(world, phase_handler=controller)
            engine.run(30)
        finally:
            native.rules("tick").python_path_only(False)
        return _state(world)

    driven = run("driven", on_the_phases=False)
    phases = run("phases", on_the_phases=True)
    assert json.dumps(driven) == json.dumps(phases)
    assert any("zone_edge_capacity_wait" in record for record in driven["records"])


# ---- A Run with a preemption ----


def test_an_urgent_request_preempts_a_routed_task_in_a_run(tmp_path: Path) -> None:
    """The Run no Scenario of the gate has: a priority comes from the Provider's boundary only."""

    def run(name: str, *, on_the_phases: bool) -> dict[str, Any]:
        document = _document(
            duration=120,
            staff=[
                _member("S01", "toilet", "support", "patrol", "cleaning"),
                _member("S02", "sleeping", "support", "cleaning"),
            ],
            configs={
                "staff_operations": {
                    "task_duration_minutes": {
                        "cleaning": 90,
                        "maintenance": 90,
                        "information_outreach": 90,
                        "intake": 90,
                        "patrol": 90,
                    },
                    "thresholds_by_zone": {
                        zone: {"cleaning_sanitation_below": 1.0} for zone in ("toilet", "sleeping")
                    },
                },
            },
        )
        scenario = ScenarioSpec.model_validate(document)
        world = WorldState.from_scenario(scenario)
        artifacts = RunArtifactWriter(scenario, tmp_path / name, trace_level=TraceLevel.OFF)
        artifacts.write_initial(world, execution_mode="offline")
        controller = OfflineSimulationController(world, artifacts)
        engine = TickEngine(world, phase_handler=controller)
        native.rules("tick").python_path_only(on_the_phases)
        try:
            for _ in range(60):
                priorities = world.model_state.setdefault("llm", {}).setdefault(
                    "support_priorities", {}
                )
                for request in world.unsettled_support_requests():
                    priorities[request.id] = 1
                engine.step()
        finally:
            native.rules("tick").python_path_only(False)
        return _state(world)

    counts = native.rules("tick").stats()[0]
    ticks = counts["ticks"]
    driven = run("driven", on_the_phases=False)
    assert native.rules("tick").stats()[0]["ticks"] == ticks + 60
    phases = run("phases", on_the_phases=True)
    assert json.dumps(driven) == json.dumps(phases)
    assert any("staff_operation_preempted" in record for record in driven["records"])
