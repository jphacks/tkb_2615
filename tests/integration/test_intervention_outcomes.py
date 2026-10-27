"""An intervention on a UI-generated Scenario changes what the operator sees.

Each test forks the main line of the shared generated Scenario, applies one
approved EffectProgram to the child Branch, advances both with the TickEngine,
and compares the KPIs the Project screen shows. It also checks the recorded
events, so the change has to come from the domain mechanism, not from a KPI.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest

from sheltersim.contracts.extensions import Point2D
from sheltersim.core.world import WorldState
from sheltersim.domain.geometry_overlay import effective_geometry
from sheltersim.domain.models import Activity
from sheltersim.domain.movement import _segment_clear_of_polygon
from sheltersim.domain.staff_dynamics import staff_dynamics_on_shift
from sheltersim.events.effects import (
    EffectProgram,
    PlanAssumption,
    SupportMode,
    TargetSelector,
    TimedAction,
)
from sheltersim.events.models import EventLogRecord, EventStatus
from sheltersim.experiments.branch import BranchError, BranchRuntime
from sheltersim.experiments.metrics import MetricSnapshot
from sheltersim.experiments.operational_kpis import operational_kpi_values
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.storage.projects import ProjectStore, digest

FORK_MINUTE = 60
OUTAGE_END_MINUTE = 300
COMPARE_MINUTE = 360


def _action(
    op: str,
    target: TargetSelector,
    parameters: dict[str, Any],
    *,
    units: str,
    policy_dependencies: list[str],
    start_minute: int = FORK_MINUTE,
    end_minute: int | None = None,
) -> TimedAction:
    return TimedAction(
        effect_id=f"{op}-1",
        op=op,
        target_selector=target,
        start_minute=start_minute,
        end_minute=end_minute,
        parameters=parameters,
        units=units,
        source_id="TEST-INTERVENTION-OUTCOME",
        policy_dependencies=policy_dependencies,
        reversible=end_minute is not None,
    )


def _program(*effects: TimedAction, assumptions: tuple[PlanAssumption, ...] = ()) -> EffectProgram:
    return EffectProgram(
        program_id="program-" + "-".join(effect.op for effect in effects),
        narrative=", ".join(effect.op for effect in effects),
        source_text=", ".join(effect.op for effect in effects),
        simulation_start_minute=min(effect.start_minute for effect in effects),
        effects=list(effects),
        assumptions=list(assumptions),
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


def _timeline(tmp_path, scenario: ScenarioSpec) -> TimelineStore:
    store = ProjectStore(tmp_path)
    store.create_project("p")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    return timeline


def _advance(timeline: TimelineStore, branch_id: str, minute: int) -> WorldState:
    # Checkpoint cadence changes only what is saved, not the simulation; saving
    # every 30 minutes would double the run time of these tests.
    return timeline.advance(branch_id, minute, checkpoint_interval=minute).world


def _fork(
    timeline: TimelineStore, source: str, minute: int, branch_id: str, program: EffectProgram
) -> None:
    timeline.fork(
        source,
        minute,
        branch_id,
        program=program,
        approved_program_hash=digest(program.model_dump(mode="json")),
    )


def _main_and_child(
    tmp_path, scenario: ScenarioSpec, program: EffectProgram, *, until: int = COMPARE_MINUTE
) -> tuple[WorldState, WorldState]:
    """Run the main line and an intervention Branch forked from it to the same minute."""
    timeline = _timeline(tmp_path, scenario)
    _advance(timeline, "main", program.simulation_start_minute)
    _fork(timeline, "main", program.simulation_start_minute, "child", program)
    child = _advance(timeline, "child", until)
    main = _advance(timeline, "main", until)
    return main, child


@pytest.fixture(scope="module")
def main_line(tmp_path_factory, generated_scenario_preview) -> TimelineStore:
    """The shared generated Scenario's main line, run once; tests fork their own Branches."""
    scenario = ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    timeline = _timeline(tmp_path_factory.mktemp("main-line"), scenario)
    # Hourly checkpoints keep forking at any minute cheap.
    timeline.advance("main", COMPARE_MINUTE, checkpoint_interval=FORK_MINUTE)
    return timeline


def _fork_main(
    main_line: TimelineStore, branch_id: str, program: EffectProgram
) -> tuple[WorldState, WorldState]:
    """Fork the shared main line with one program and run the Branch to COMPARE_MINUTE."""
    _fork(main_line, "main", program.simulation_start_minute, branch_id, program)
    return main_line.seek("main", COMPARE_MINUTE).world, _advance(
        main_line, branch_id, COMPARE_MINUTE
    )


def _first_minute(
    timeline: TimelineStore,
    branch_id: str,
    start: int,
    end: int,
    condition: Callable[[WorldState], bool],
) -> int:
    """The first computed minute of a Branch where condition holds; read-only."""
    for world in timeline.iter_worlds(branch_id, start, end):
        if condition(world):
            return world.minute_index
    raise AssertionError(f"no minute in {start}..{end} of {branch_id} matches")


def _kpis(world: WorldState) -> dict[str, Any]:
    snapshot = MetricSnapshot.from_world(world)
    return {
        **{name: getattr(snapshot, name) for name in snapshot.__dataclass_fields__},
        **operational_kpi_values(world, snapshot),
    }


def _applied(world: WorldState, event_type: str, **payload: Any) -> list[EventLogRecord]:
    return [
        record
        for record in world.event_log.records
        if record.event.event_type == event_type
        and record.status is EventStatus.APPLIED
        and all(record.event.payload.get(key) == value for key, value in payload.items())
    ]


def _completed_between(
    world: WorldState, event_type: str, first: int, last: int, **payload: Any
) -> list[EventLogRecord]:
    return [
        record
        for record in _applied(world, event_type, **payload)
        if first <= record.realized_minute <= last
    ]


def test_added_staff_shorten_the_wait_caused_by_busy_staff(main_line) -> None:
    program = _program(
        _action(
            "add_staff",
            TargetSelector(kind="zone", target_id="staff"),
            {"count": 2},
            units="staff_member",
            policy_dependencies=["staff_operations"],
        )
    )

    main, child = _fork_main(main_line, "added-staff", program)

    added = set(child.staff) - set(main.staff)
    assert len(added) == 2
    assert _applied(child, "effect_program_structure")
    assert any(
        record.event.payload["staff_id"] in added
        for record in _applied(child, "staff_support_complete")
    ), "no support was given by an added staff member"
    assert (
        _kpis(child)["support_wait_staff_busy_request_minutes"]
        < _kpis(main)["support_wait_staff_busy_request_minutes"]
    )


def test_a_toilet_outage_leaves_toilet_needs_unmet(main_line, generated_scenario) -> None:
    program = _program(
        _action(
            "facility_outage",
            TargetSelector(kind="facility", target_id="toilet_01"),
            {"units": 1},
            units="facility_unit",
            policy_dependencies=["facility_capacity"],
            end_minute=OUTAGE_END_MINUTE,
        )
    )
    # A use that started before the outage may still finish inside it.
    first = FORK_MINUTE + generated_scenario.model_configs["toilet_service"]["maximum_minutes"]

    main, child = _fork_main(main_line, "toilet-outage", program)

    assert _applied(child, "effect_program_apply")
    assert _applied(child, "effect_program_expire")
    assert child.facilities["toilet_01"].working_units == main.facilities["toilet_01"].working_units
    in_outage = {"first": first, "last": OUTAGE_END_MINUTE, "facility_id": "toilet_01"}
    assert _completed_between(main, "toilet_service_complete", **in_outage)
    assert not _completed_between(child, "toilet_service_complete", **in_outage)
    assert len(_applied(child, "toilet_service_complete")) < len(
        _applied(main, "toilet_service_complete")
    )
    assert _kpis(child)["toilet_urgency_mean"] > _kpis(main)["toilet_urgency_mean"]


def test_a_water_outage_stops_water_service_and_leaves_people_thirsty(
    tmp_path, generated_scenario_with
) -> None:
    # Only Scenarios long enough to schedule a supply delivery (4 days or more)
    # accept a water outage today; shorter ones reject it before the Branch
    # exists (#65). The comparison still covers only the first 6 hours.
    scenario = generated_scenario_with(duration_days=4)
    program = _program(
        _action(
            "water_outage",
            TargetSelector(kind="facility", target_id="water_station_main"),
            {"outage": True},
            units="water_supply",
            policy_dependencies=["operational_events_v1", "resource_service_v1"],
            end_minute=OUTAGE_END_MINUTE,
        )
    )
    first = FORK_MINUTE + scenario.model_configs["resource_service"]["service_minutes"]

    main, child = _main_and_child(tmp_path, scenario, program)

    assert _applied(child, "water_outage")
    assert _applied(child, "water_restored")
    in_outage = {
        "first": first,
        "last": OUTAGE_END_MINUTE,
        "facility_id": "water_station_main",
        "resource_id": "water_ml",
    }
    assert _completed_between(main, "resource_service_complete", **in_outage)
    assert not _completed_between(child, "resource_service_complete", **in_outage)
    assert _kpis(child)["water_outage_stall_count"] > _kpis(main)["water_outage_stall_count"]
    assert _kpis(child)["hydration_need_mean"] > _kpis(main)["hydration_need_mean"]


def test_a_water_delivery_relieves_thirst_once_stock_has_run_out(
    tmp_path, generated_scenario_with
) -> None:
    # The operator entered 1 litre of water, so the stock runs out within hours.
    scenario = generated_scenario_with(
        operational_conditions={
            "initial_resources": {
                "status": "entered",
                "food_meal_units": 180,
                "water_ml": 1_000,
                "source_kind": "operator",
            }
        }
    )
    program = _program(
        _action(
            "deliver_resource",
            TargetSelector(kind="resource", target_id="water_ml"),
            {"warehouse_id": "main_warehouse", "resource_id": "water_ml", "quantity": 100_000},
            units="water_ml",
            policy_dependencies=["resource_ledger_v1"],
        )
    )

    main, child = _main_and_child(tmp_path, scenario, program)

    assert _applied(child, "resource_delivery", resource_id="water_ml", quantity=100_000)
    assert _kpis(main)["water_ml_remaining"] == 0
    assert _kpis(child)["water_ml_remaining"] > 0
    assert len(_applied(child, "resource_service_complete", resource_id="water_ml")) > len(
        _applied(main, "resource_service_complete", resource_id="water_ml")
    )
    assert _kpis(child)["hydration_need_mean"] < _kpis(main)["hydration_need_mean"]


def test_capping_a_toilet_at_zero_units_leaves_toilet_needs_unmet(
    main_line, generated_scenario
) -> None:
    program = _program(
        _action(
            "set_facility_availability",
            TargetSelector(kind="facility", target_id="toilet_01"),
            {"working_units": 0},
            units="facility_unit",
            policy_dependencies=["facility_capacity"],
            end_minute=OUTAGE_END_MINUTE,
        )
    )
    first = FORK_MINUTE + generated_scenario.model_configs["toilet_service"]["maximum_minutes"]

    main, child = _fork_main(main_line, "toilet-cap", program)

    assert _applied(child, "effect_program_apply")
    assert _applied(child, "effect_program_expire")
    in_cap = {"first": first, "last": OUTAGE_END_MINUTE, "facility_id": "toilet_01"}
    assert _completed_between(main, "toilet_service_complete", **in_cap)
    assert not _completed_between(child, "toilet_service_complete", **in_cap)
    assert _kpis(child)["toilet_urgency_mean"] > _kpis(main)["toilet_urgency_mean"]


# The operator reported toilet_01 out of order until minute 300, so only the
# accessible toilet works and toilet waits grow.
REPORTED_TOILET_OUTAGE = {
    "operational_conditions": {
        "scheduled": [
            {
                "kind": "facility_outage",
                "facility_id": "toilet_01",
                "start_minute": 0,
                "end_minute": OUTAGE_END_MINUTE,
                "units_out": 1,
            }
        ]
    }
}


def test_repairing_a_reported_broken_toilet_shortens_toilet_waits(
    tmp_path, generated_scenario_with
) -> None:
    scenario = generated_scenario_with(**REPORTED_TOILET_OUTAGE)
    program = _program(
        _action(
            "facility_repair",
            TargetSelector(kind="facility", target_id="toilet_01"),
            {
                "facility_id": "toilet_01",
                "component_id": "toilet_01",
                "expected_working_units": 0,
                "restored_working_units": 1,
            },
            units="facility_unit",
            policy_dependencies=["facility_capacity", "shared_operations_v1"],
        )
    )

    main, child = _main_and_child(tmp_path, scenario, program)

    assert _applied(child, "effect_program_structure")
    before_recovery = {"first": FORK_MINUTE, "last": OUTAGE_END_MINUTE, "facility_id": "toilet_01"}
    assert not _completed_between(main, "toilet_service_complete", **before_recovery)
    assert _completed_between(child, "toilet_service_complete", **before_recovery)
    assert child.facilities["toilet_01"].working_units == child.facilities["toilet_01"].capacity
    assert _kpis(child)["toilet_wait_total_minutes"] < _kpis(main)["toilet_wait_total_minutes"]


def test_added_toilets_shorten_toilet_waits_while_a_toilet_is_broken(
    tmp_path, generated_scenario_with
) -> None:
    # With every toilet working, 20 people rarely queue, so the shortage shows
    # what an operator adds toilets for.
    scenario = generated_scenario_with(**REPORTED_TOILET_OUTAGE)
    program = _program(
        _action(
            "add_toilet",
            TargetSelector(kind="zone", target_id="toilet"),
            {"count": 2},
            units="toilet_stall",
            policy_dependencies=["facility_capacity"],
        )
    )

    main, child = _main_and_child(tmp_path, scenario, program)

    added = set(child.facilities) - set(main.facilities)
    assert len(added) == 2
    assert _applied(child, "effect_program_structure")
    assert any(
        record.event.payload["facility_id"] in added
        for record in _applied(child, "toilet_service_complete")
    ), "nobody used an added toilet"
    assert len(_applied(child, "toilet_service_complete")) > len(
        _applied(main, "toilet_service_complete")
    )
    assert _kpis(child)["toilet_wait_mean_minutes"] < _kpis(main)["toilet_wait_mean_minutes"]
    assert _kpis(child)["toilet_urgency_mean"] < _kpis(main)["toilet_urgency_mean"]


@pytest.fixture(scope="module")
def staff_absence(tmp_path_factory, generated_previews) -> tuple[WorldState, WorldState]:
    """The only support worker is away from minute 60 to 300.

    Only Scenarios long enough to schedule a supply delivery accept a staff
    absence today (#65), so this uses four days and compares the first 6 hours.
    """
    scenario = ScenarioSpec.model_validate(
        copy.deepcopy(generated_previews(duration_days=4)["scenario"])
    )
    program = _program(
        _action(
            "set_staff_availability",
            TargetSelector(kind="staff", target_id="staff_001"),
            {"available": False},
            units="staff_member",
            policy_dependencies=["operational_events_v1"],
            end_minute=OUTAGE_END_MINUTE,
        )
    )
    return _main_and_child(tmp_path_factory.mktemp("staff-absence"), scenario, program)


def test_a_staff_absence_stops_support_and_lets_requests_time_out(staff_absence) -> None:
    main, child = staff_absence
    first = FORK_MINUTE + child.model_configs["staff"]["support_task_minutes"]

    assert _applied(child, "staff_unavailable")
    assert _applied(child, "staff_restored")
    away = {"first": first, "last": OUTAGE_END_MINUTE, "staff_id": "staff_001"}
    assert _completed_between(main, "staff_support_complete", **away)
    assert not _completed_between(child, "staff_support_complete", **away)
    assert len(_applied(child, "staff_support_complete")) < len(
        _applied(main, "staff_support_complete")
    )
    assert len(_applied(child, "support_request_timeout")) > len(
        _applied(main, "support_request_timeout")
    )


def test_a_staff_absence_shows_in_the_support_wait_reasons(staff_absence) -> None:
    main, child = staff_absence
    reasons = (
        "support_wait_staff_busy_request_minutes",
        "support_wait_staff_off_shift_request_minutes",
        "support_no_qualified_worker_request_minutes",
    )

    assert sum(_kpis(child)[name] for name in reasons) > sum(_kpis(main)[name] for name in reasons)


def test_giving_a_water_worker_the_support_role_shortens_support_waits(main_line) -> None:
    program = _program(
        _action(
            "set_staff_role",
            TargetSelector(kind="staff", target_id="staff_002"),
            {"skills": ["support", "water_service", "meal_service"]},
            units="staff_role",
            policy_dependencies=["staff_operations_v1"],
        )
    )

    main, child = _fork_main(main_line, "support-role", program)

    assert _applied(child, "staff_support_complete", staff_id="staff_002")
    assert (
        _kpis(child)["support_wait_staff_busy_request_minutes"]
        < _kpis(main)["support_wait_staff_busy_request_minutes"]
    )


def _idle(staff_id: str) -> Callable[[WorldState], bool]:
    def check(world: WorldState) -> bool:
        return world.staff[staff_id].active_task_id is None and staff_dynamics_on_shift(
            world, staff_id, world.minute_index
        )

    return check


def _patrol_order(staff_id: str, start_minute: int) -> TimedAction:
    return _action(
        "staff_task_order",
        TargetSelector(kind="staff", target_id=staff_id),
        {
            "task_kind": "patrol",
            "target_zone_id": "sleeping",
            "reason": "Reassure the sleeping area",
        },
        units="staff_task_order",
        policy_dependencies=["shared_operations_v1", "staff_operations_v1"],
        start_minute=start_minute,
    )


def test_a_patrol_order_reassures_people_at_the_cost_of_support(main_line) -> None:
    # Only staff_001 has a skill for an orderable task in a generated Scenario
    # (#75), and an order needs the worker idle, so the fork waits for that.
    start = _first_minute(main_line, "main", FORK_MINUTE, COMPARE_MINUTE, _idle("staff_001"))

    main, child = _fork_main(main_line, "patrol", _program(_patrol_order("staff_001", start)))

    assert _applied(child, "staff_operation_complete", staff_id="staff_001", task_kind="patrol")
    assert _kpis(child)["stress_mean"] < _kpis(main)["stress_mean"]
    assert (
        _kpis(child)["support_wait_staff_busy_request_minutes"]
        > _kpis(main)["support_wait_staff_busy_request_minutes"]
    )


def test_handing_off_a_patrol_frees_the_original_worker_sooner(
    main_line, generated_scenario
) -> None:
    generated_scenario_staff = {staff.id for staff in generated_scenario.staff}
    timeline = main_line
    start = _first_minute(timeline, "main", FORK_MINUTE, COMPARE_MINUTE, _idle("staff_001"))
    # A second worker waits in the sleeping area while staff_001 patrols it.
    setup = _program(
        _action(
            "add_staff",
            TargetSelector(kind="zone", target_id="sleeping"),
            {"count": 1},
            units="staff_member",
            policy_dependencies=["staff_operations"],
            start_minute=start,
        ),
        _patrol_order("staff_001", start),
    )
    _fork(timeline, "main", start, "handoff-setup", setup)
    _advance(timeline, "handoff-setup", COMPARE_MINUTE)

    def patrolling(world: WorldState) -> bool:
        record = world.model_state["staff_operations_v1"]["staff"]["staff_001"]
        return record.get("phase") == "working"

    minute = _first_minute(timeline, "handoff-setup", start, COMPARE_MINUTE, patrolling)
    world = timeline.seek("handoff-setup", minute).world
    (replacement,) = set(world.staff) - set(generated_scenario_staff)
    handoff = _program(
        _action(
            "staff_handoff",
            TargetSelector(kind="staff", target_id="staff_001"),
            {
                "staff_id": "staff_001",
                "replacement_staff_id": replacement,
                "task_id": world.model_state["staff_operations_v1"]["staff"]["staff_001"][
                    "task_id"
                ],
                "reason_code": "reassigned",
            },
            units="staff_member",
            policy_dependencies=["shared_operations_v1", "staff_operations_v1"],
            start_minute=minute,
        )
    )
    _fork(timeline, "handoff-setup", minute, "handoff", handoff)

    handed = _advance(timeline, "handoff", COMPARE_MINUTE)
    kept = _advance(timeline, "handoff-setup", COMPARE_MINUTE)

    def next_support_by_staff_001(world: WorldState) -> int:
        return min(
            record.realized_minute
            for record in _applied(world, "staff_support_complete", staff_id="staff_001")
            if record.realized_minute > minute
        )

    assert _applied(handed, "staff_handoff")
    assert _applied(handed, "staff_operation_complete", staff_id=replacement, task_kind="patrol")
    assert _applied(kept, "staff_operation_complete", staff_id="staff_001", task_kind="patrol")
    assert next_support_by_staff_001(handed) < next_support_by_staff_001(kept)


def test_delaying_a_water_delivery_keeps_people_thirsty_longer(
    tmp_path, generated_scenario_with
) -> None:
    # The operator entered 1 litre of water and a 100 L delivery at minute 120.
    scenario = generated_scenario_with(
        operational_conditions={
            "initial_resources": {
                "status": "entered",
                "food_meal_units": 180,
                "water_ml": 1_000,
                "source_kind": "operator",
            },
            "scheduled": [
                {
                    "kind": "resource_delivery",
                    "resource_id": "water_ml",
                    "quantity": 100_000,
                    "due_minute": 120,
                    "warehouse_id": "main_warehouse",
                }
            ],
        }
    )
    shipments = WorldState.from_scenario(scenario).model_state["operational_events_v1"]["shipments"]
    shipment_id = next(key for key, row in shipments.items() if row["resource_id"] == "water_ml")
    program = _program(
        _action(
            "modify_supply_schedule",
            TargetSelector(kind="shipment", target_id=shipment_id),
            {"shipment_id": shipment_id, "to_minute": OUTAGE_END_MINUTE},
            units="minute",
            policy_dependencies=["operational_events_v1"],
        )
    )

    main, child = _main_and_child(tmp_path, scenario, program)

    assert [record.realized_minute for record in _applied(main, "resource_delivery")] == [120]
    assert [record.realized_minute for record in _applied(child, "resource_delivery")] == [
        OUTAGE_END_MINUTE
    ]
    assert _kpis(child)["hydration_need_mean"] > _kpis(main)["hydration_need_mean"]


def test_arriving_evacuees_raise_toilet_and_water_demand(main_line) -> None:
    program = _program(
        _action(
            "add_evacuees",
            TargetSelector(kind="zone", target_id="sleeping"),
            {"count": 10},
            units="evacuee",
            policy_dependencies=["population_dynamics_v1"],
        )
    )

    main, child = _fork_main(main_line, "arrivals", program)

    assert _applied(child, "effect_program_population")
    assert len(child.agents) == len(main.agents) + 10
    assert len(_applied(child, "toilet_service_complete")) > len(
        _applied(main, "toilet_service_complete")
    )
    assert _kpis(child)["toilet_wait_total_minutes"] > _kpis(main)["toilet_wait_total_minutes"]
    assert _kpis(child)["water_ml_remaining"] < _kpis(main)["water_ml_remaining"]


def test_departing_evacuees_lower_toilet_demand(main_line) -> None:
    program = _program(
        _action(
            "depart_evacuees",
            TargetSelector(kind="zone", target_id="sleeping"),
            {"count": 5, "include_initial": True},
            units="evacuee",
            policy_dependencies=["population_dynamics_v1"],
        )
    )

    main, child = _fork_main(main_line, "departures", program)

    assert len(child.agents) == len(main.agents) - 5
    assert len(_applied(child, "toilet_service_complete")) < len(
        _applied(main, "toilet_service_complete")
    )


def _service_users(world: WorldState) -> set[str]:
    return {row["agent_id"] for row in world.model_state["resource_service_v1"]["request_history"]}


def _temporary_exit(agent_id: str) -> EffectProgram:
    return _program(
        _action(
            "population_presence",
            TargetSelector(kind="person", target_id=agent_id),
            {"action": "temporary_exit", "expected_return_minute": OUTAGE_END_MINUTE},
            units="population_transition",
            policy_dependencies=["population_lifecycle_v1"],
        )
    )


def test_a_temporary_exit_takes_the_person_out_of_the_shelter(main_line) -> None:
    # Someone who has not used water or meal service yet; the others fail (#79).
    at_fork = main_line.seek("main", FORK_MINUTE).world
    agent_id = min(set(at_fork.agents) - _service_users(at_fork))

    main, child = _fork_main(main_line, "temporary-exit", _temporary_exit(agent_id))

    assert _applied(child, "effect_program_population")
    assert agent_id in main.agents
    assert agent_id not in child.agents
    assert not [
        record
        for record in child.event_log.records
        if record.realized_minute > FORK_MINUTE and record.event.payload.get("agent_id") == agent_id
    ], "the person still acted in the shelter while away"


def test_someone_who_has_used_water_or_meals_can_go_out_and_come_back(main_line) -> None:
    # Before #79 was fixed their service history made the exit fail validation.
    at_fork = main_line.seek("main", FORK_MINUTE).world
    agent_id = min(_service_users(at_fork))
    used = [
        row
        for row in at_fork.model_state["resource_service_v1"]["request_history"]
        if row["agent_id"] == agent_id
    ]
    return_minute = 200
    back = _program(
        _action(
            "population_presence",
            TargetSelector(kind="person", target_id=agent_id),
            {"action": "return"},
            units="population_transition",
            policy_dependencies=["population_lifecycle_v1"],
            start_minute=return_minute,
        )
    ).model_copy(update={"program_id": "program-return"})

    _fork_main(main_line, "temporary-exit-user", _temporary_exit(agent_id))
    away = main_line.seek("temporary-exit-user", return_minute).world
    _fork(main_line, "temporary-exit-user", return_minute, "returned-user", back)
    returned = _advance(main_line, "returned-user", COMPARE_MINUTE)

    assert agent_id not in away.agents
    assert agent_id in returned.agents
    assert [
        record.realized_minute for record in _applied(returned, "effect_program_population")
    ] == [
        FORK_MINUTE,
        return_minute,
    ]
    history = returned.model_state["resource_service_v1"]["request_history"]
    assert all(row in history for row in used), "the earlier water and meal records were lost"


@pytest.mark.xfail(
    strict=True,
    raises=BranchError,
    reason="#80: generated Scenarios do not enable the temporary space state",
)
def test_a_temporary_space_is_opened_for_its_window(main_line) -> None:
    # What the extra capacity should change for operators is to be decided once
    # it applies; until then this checks only that it opens and closes.
    program = _program(
        _action(
            "add_temporary_space",
            TargetSelector(kind="zone", target_id="sleeping"),
            {"people_capacity": 10, "sleeping_capacity": 10},
            units="person_capacity",
            policy_dependencies=["temporary_spaces_v1"],
            end_minute=OUTAGE_END_MINUTE,
        )
    )

    main, child = _fork_main(main_line, "temporary-space", program)

    applied_ops = [
        record.event.payload.get("op")
        for record in child.event_log.records
        if record.status is EventStatus.APPLIED
    ]
    assert applied_ops.count("add_temporary_space") >= 2


def _geometry_action(
    op: str,
    target: TargetSelector,
    parameters: dict[str, Any],
    *,
    start_minute: int = FORK_MINUTE,
    end_minute: int | None = None,
) -> TimedAction:
    return _action(
        op,
        target,
        parameters,
        units="boolean" if op == "set_route_access" else "metre_2d",
        policy_dependencies=["geometry_overlay_v1"],
        start_minute=start_minute,
        end_minute=end_minute,
    )


def _facility_idle(facility_id: str) -> Callable[[WorldState], bool]:
    def check(world: WorldState) -> bool:
        facility = world.facilities[facility_id]
        return not any(
            (
                facility.queue_agent_ids,
                facility.active_agent_ids,
                facility.resource_queue_agent_ids,
                facility.resource_active_agent_ids,
            )
        )

    return check


def _trips_to(world: WorldState, facility_id: str, after: int) -> list[dict[str, Any]]:
    return [
        record.event.payload
        for record in _applied(world, "agent_travel_complete", facility_id=facility_id)
        if record.realized_minute > after
    ]


def test_closing_the_route_into_the_toilets_stops_toilet_use_until_it_reopens(
    main_line, generated_scenario
) -> None:
    assumption = PlanAssumption(
        assumption_id="ASM-TEST-ROUTE-CLOSURE-V1",
        description="The corridor door to the toilets is closed for cleaning.",
        source_id="TEST-INTERVENTION-OUTCOME",
    )
    closure = _geometry_action(
        "set_route_access",
        TargetSelector(kind="route", target_id="route_corridor_toilet"),
        {"accessible": False},
        end_minute=OUTAGE_END_MINUTE,
    ).model_copy(update={"assumption_ids": [assumption.assumption_id]})
    program = _program(closure, assumptions=(assumption,))
    # Someone already inside the toilet area may still finish a use.
    first = FORK_MINUTE + generated_scenario.model_configs["toilet_service"]["maximum_minutes"]

    main, child = _fork_main(main_line, "route-closure", program)

    assert _applied(child, "effect_program_geometry", op="set_route_access")
    assert _applied(child, "effect_program_geometry_expire", op="set_route_access")
    middle = (FORK_MINUTE + OUTAGE_END_MINUTE) // 2
    assert main_line.seek("route-closure", middle).world.zone_edges["route_corridor_toilet"].closed
    assert not child.zone_edges["route_corridor_toilet"].closed
    closed = {"first": first, "last": OUTAGE_END_MINUTE}
    assert _completed_between(main, "toilet_service_complete", **closed)
    assert not _completed_between(child, "toilet_service_complete", **closed)
    assert _kpis(child)["toilet_wait_total_minutes"] > _kpis(main)["toilet_wait_total_minutes"]
    # The floor-plan history names the assumption behind the closure (#91).
    history = child.model_state["geometry_overlay_v1"]["history"]
    assert [row["status"] for row in history if row["op"] == "set_route_access"] == [
        "applied",
        "expired",
    ]
    assert all(
        assumption.assumption_id in row["source_ids"]
        for row in history
        if row["op"] == "set_route_access"
    )


def test_closing_a_route_stops_a_worker_walking_on_it(main_line) -> None:
    # Before #91 was fixed the worker walked on through the closed route.
    start = _first_minute(main_line, "main", FORK_MINUTE, COMPARE_MINUTE, _idle("staff_001"))
    _fork(main_line, "main", start, "walk", _program(_patrol_order("staff_001", start)))
    _advance(main_line, "walk", COMPARE_MINUTE)

    def walking(world: WorldState) -> bool:
        record = world.model_state["staff_operations_v1"]["staff"]["staff_001"]
        return record.get("phase") == "travel"

    minute = _first_minute(main_line, "walk", start, COMPARE_MINUTE, walking)
    record = main_line.seek("walk", minute).world.model_state["staff_operations_v1"]["staff"][
        "staff_001"
    ]
    closure = _program(
        _geometry_action(
            "set_route_access",
            TargetSelector(kind="route", target_id=record["route_edge_ids"][-1]),
            {"accessible": False},
            start_minute=minute,
            end_minute=minute + FORK_MINUTE,
        )
    )
    _fork(main_line, "walk", minute, "walk-closed", closure)

    closed = _advance(main_line, "walk-closed", COMPARE_MINUTE)
    kept = main_line.seek("walk", COMPARE_MINUTE).world

    def patrols(world: WorldState) -> list[tuple[str, int]]:
        return [
            (row["status"], row["complete_minute"])
            for row in world.model_state["staff_operations_v1"]["history"]
            if row["task_id"] == record["task_id"]
        ]

    (route_event,) = _applied(closed, "effect_program_geometry", op="set_route_access")
    assert patrols(closed) == [("interrupted", minute)]
    assert route_event.event.event_id in next(
        row["causation_event_ids"]
        for row in closed.model_state["staff_operations_v1"]["history"]
        if row["task_id"] == record["task_id"]
    )
    assert [status for status, _ in patrols(kept)] == ["completed"]


def test_moving_a_toilet_closer_to_the_door_changes_where_people_walk(
    main_line, generated_scenario
) -> None:
    start = _first_minute(
        main_line, "main", FORK_MINUTE, COMPARE_MINUTE, _facility_idle("toilet_01")
    )
    # Same toilet area, just inside the door instead of at the back wall.
    old = generated_scenario.model_configs["facility_geometry"]["facility_points"]["toilet_01"]
    point = {"x": old["x"], "y": 8.0}
    program = _program(
        _geometry_action(
            "move_facility",
            TargetSelector(kind="facility", target_id="toilet_01"),
            {"point_m": point},
            start_minute=start,
        )
    )

    main, child = _fork_main(main_line, "move-toilet", program)

    assert _applied(child, "effect_program_geometry", op="move_facility")
    assert effective_geometry(child).facility_points["toilet_01"].model_dump() == point
    assert effective_geometry(main).facility_points["toilet_01"].model_dump() == old
    assert _completed_between(
        child, "toilet_service_complete", start, COMPARE_MINUTE, facility_id="toilet_01"
    )


def test_bringing_a_toilet_into_the_sleeping_area_shortens_the_walk_to_it(main_line) -> None:
    start = _first_minute(
        main_line, "main", FORK_MINUTE, COMPARE_MINUTE, _facility_idle("toilet_01")
    )
    program = _program(
        _geometry_action(
            "transfer_equipment",
            TargetSelector(kind="equipment", target_id="toilet_01"),
            {
                "schema_version": 1,
                "equipment_id": "toilet_01",
                "from_zone_id": "toilet",
                "to_zone_id": "sleeping",
                "point_m": {"x": 5.0, "y": 9.0},
            },
            start_minute=start,
        )
    )

    main, child = _fork_main(main_line, "transfer-toilet", program)

    assert _applied(child, "effect_program_geometry", op="transfer_equipment")
    assert child.facilities["toilet_01"].zone_id == "sleeping"
    assert _completed_between(
        child, "toilet_service_complete", start, COMPARE_MINUTE, facility_id="toilet_01"
    )
    trips = _trips_to(child, "toilet_01", start)
    assert all(trip["route_zone_ids"][-1] == "sleeping" for trip in trips)
    assert any(
        trip["route_zone_ids"] == ("sleeping", "sleeping") and trip["purpose"] == "toilet_request"
        for trip in trips
    ), "nobody in the sleeping area walked straight to the toilet"
    assert sum(trip["travel_minutes"] for trip in trips) < sum(
        trip["travel_minutes"] for trip in _trips_to(main, "toilet_01", start)
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#83: the water staff stay in the old area, so nobody serves the moved station",
)
def test_bringing_the_water_station_into_the_sleeping_area_keeps_water_served(
    main_line, generated_scenario
) -> None:
    start = _first_minute(
        main_line, "main", FORK_MINUTE, COMPARE_MINUTE, _facility_idle("water_station_main")
    )
    program = _program(
        _geometry_action(
            "transfer_equipment",
            TargetSelector(kind="equipment", target_id="water_station_main"),
            {
                "schema_version": 1,
                "equipment_id": "water_station_main",
                "from_zone_id": "food",
                "to_zone_id": "sleeping",
                "point_m": {"x": 5.0, "y": 9.0},
            },
            start_minute=start,
        )
    )

    main, child = _fork_main(main_line, "transfer-water", program)

    assert _applied(child, "effect_program_geometry", op="transfer_equipment")
    assert _completed_between(
        child,
        "resource_service_complete",
        start,
        COMPARE_MINUTE,
        facility_id="water_station_main",
    )
    assert _kpis(child)["hydration_need_mean"] <= _kpis(main)["hydration_need_mean"]


def _walks_through(
    timeline: TimelineStore, branch_id: str, first: int, last: int, polygon: tuple[Point2D, ...]
) -> bool:
    """Whether a walk planned in first..last passes through polygon; read-only."""
    for world in timeline.iter_worlds(branch_id, first, last):
        for journey in world.model_state["movement_v1"]["journeys"].values():
            points = [Point2D.model_validate(point) for point in journey.get("route_points", ())]
            if any(
                not _segment_clear_of_polygon(a, b, polygon, 0.0)
                for a, b in zip(points, points[1:])
            ):
                return True
    return False


def test_people_walk_around_a_partition_while_it_stands(main_line) -> None:
    # Between the toilet area's door and the toilets, leaving a gap by the wall.
    polygon = [
        {"x": 13.9, "y": 8.4},
        {"x": 14.9, "y": 8.4},
        {"x": 14.9, "y": 9.0},
        {"x": 13.9, "y": 9.0},
    ]
    program = _program(
        _geometry_action(
            "set_partition",
            TargetSelector(kind="zone", target_id="toilet"),
            {"polygon_m": polygon},
            end_minute=OUTAGE_END_MINUTE,
        )
    )

    main, child = _fork_main(main_line, "partition", program)

    assert _applied(child, "effect_program_geometry", op="set_partition")
    assert _applied(child, "effect_program_geometry_expire", op="set_partition")
    shape = tuple(Point2D.model_validate(point) for point in polygon)
    standing = (FORK_MINUTE + 1, OUTAGE_END_MINUTE - 1)
    assert _walks_through(main_line, "main", *standing, shape)
    assert not _walks_through(main_line, "partition", *standing, shape)
    assert len(_applied(child, "toilet_service_complete")) == len(
        _applied(main, "toilet_service_complete")
    ), "a partition beside the path should not stop anyone using the toilets"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#84: people walk to a temporary room's bed but do not sleep there",
)
def test_people_who_go_to_sleep_use_the_beds_of_a_temporary_room(main_line) -> None:
    beds = [
        {"x": 1.0, "y": 9.0},
        {"x": 2.0, "y": 9.0},
        {"x": 1.0, "y": 11.0},
        {"x": 2.0, "y": 11.0},
    ]
    program = _program(
        _geometry_action(
            "temporary_room_geometry",
            TargetSelector(kind="zone", target_id="sleeping"),
            {
                "schema_version": 1,
                "room_id": "room_a",
                "zone_id": "sleeping",
                "polygon_m": [
                    {"x": 0.5, "y": 8.0},
                    {"x": 2.5, "y": 8.0},
                    {"x": 2.5, "y": 12.0},
                    {"x": 0.5, "y": 12.0},
                ],
                "people_capacity": 4,
                "sleeping_capacity": 4,
                "bed_points_m": beds,
                "route_edge_ids": ["route_corridor_sleeping", "route_sleeping_corridor"],
            },
            end_minute=OUTAGE_END_MINUTE,
        )
    )

    main, child = _fork_main(main_line, "temporary-room", program)

    assert _applied(child, "effect_program_geometry", op="temporary_room_geometry")
    assert _applied(child, "agent_travel_complete", purpose="sleep_bed")
    sleeping_in_a_bed = 0
    for world in main_line.iter_worlds("temporary-room", FORK_MINUTE + 1, OUTAGE_END_MINUTE - 1):
        positions = world.model_state["movement_v1"]["positions"]
        sleeping_in_a_bed += sum(
            agent.activity is Activity.SLEEPING and positions[agent_id]["point"] in beds
            for agent_id, agent in world.agents.items()
        )
    assert sleeping_in_a_bed > 0


def _environment_action(op: str, parameters: dict[str, Any], units: str) -> TimedAction:
    return _action(
        op,
        TargetSelector(kind="zone", target_id="sleeping"),
        parameters,
        units=units,
        policy_dependencies=["zone_environment_v1"],
        end_minute=OUTAGE_END_MINUTE,
    )


def _environment_branch(
    main_line: TimelineStore, branch_id: str, action: TimedAction
) -> tuple[WorldState, WorldState]:
    """Fork with one sleeping-area condition and compare both lines as it expires.

    A condition changes how people's state builds up while it lasts, so the
    lines are compared at its end, before the difference fades.
    """
    _fork(main_line, "main", FORK_MINUTE, branch_id, _program(action))
    child = _advance(main_line, branch_id, OUTAGE_END_MINUTE)
    main = main_line.seek("main", OUTAGE_END_MINUTE).world
    started = _applied(child, "effect_program_environment", op=action.op, zone_id="sleeping")
    assert [record.realized_minute for record in started] == [FORK_MINUTE]
    # Still in effect: its expiry is due at this minute and not yet applied.
    (modifier,) = child.model_state["zone_environment_v1"]["modifiers"].values()
    assert (modifier["op"], modifier["end_minute"]) == (action.op, OUTAGE_END_MINUTE)
    return main, child


def _mean_need(world: WorldState, name: str) -> float:
    return sum(getattr(agent.needs, name) for agent in world.agents.values()) / len(world.agents)


def _mean_body(world: WorldState, name: str) -> float:
    body = world.model_state["body_state_v1"]["agents"]
    return sum(body[agent_id][name] for agent_id in world.agents) / len(world.agents)


def test_noise_in_the_sleeping_area_leaves_people_more_tired(main_line) -> None:
    main, child = _environment_branch(
        main_line,
        "noise",
        _environment_action("set_ambient_noise", {"level": 1.0}, "normalized_noise_0_1"),
    )

    assert _mean_need(child, "fatigue") > _mean_need(main, "fatigue")


def test_bright_light_in_the_sleeping_area_leaves_people_more_tired(main_line) -> None:
    main, child = _environment_branch(
        main_line,
        "lighting",
        _environment_action("set_lighting", {"level": 1.0}, "normalized_lighting_0_1"),
    )

    assert _mean_need(child, "fatigue") > _mean_need(main, "fatigue")


def test_poor_ventilation_in_the_sleeping_area_raises_hygiene_needs(main_line) -> None:
    main, child = _environment_branch(
        main_line,
        "ventilation",
        _environment_action(
            "set_ventilation", {"effectiveness": 0.0}, "relative_ventilation_effectiveness_0_1"
        ),
    )

    assert _mean_body(child, "hygiene_need") > _mean_body(main, "hygiene_need")


def test_no_privacy_in_the_sleeping_area_raises_discontent(main_line) -> None:
    main, child = _environment_branch(
        main_line,
        "privacy",
        _environment_action("set_privacy", {"level": 0.0}, "normalized_privacy_0_1"),
    )

    assert _mean_body(child, "discontent") > _mean_body(main, "discontent")


def test_heat_in_the_sleeping_area_raises_stress(main_line) -> None:
    main, child = _environment_branch(
        main_line,
        "temperature",
        _environment_action("set_temperature", {"temperature_c": 35.0}, "celsius"),
    )

    assert _kpis(child)["stress_mean"] > _kpis(main)["stress_mean"]


def test_encouraging_rest_in_the_sleeping_area_lengthens_rests_and_relieves_fatigue(
    main_line,
) -> None:
    # Toilet visits rarely start from the sleeping area in this Scenario, so a
    # toilet multiplier there changes no choice; rest is often a choice there.
    main, child = _environment_branch(
        main_line,
        "encourage-rest",
        _environment_action(
            "modify_demand_rate",
            {"action": "rest", "probability_multiplier": 2.0, "duration_multiplier": 2.0},
            "relative_action_rate",
        ),
    )

    def mean_rest_minutes(world: WorldState) -> float:
        rests = [
            record.event.payload["duration_minutes"]
            for record in _applied(world, "agent_action_lifecycle", action="rest")
            if record.event.payload["transition"] != "started"
            and record.event.payload["start_minute"] >= FORK_MINUTE
        ]
        return sum(rests) / len(rests)

    assert mean_rest_minutes(child) > mean_rest_minutes(main)
    assert _mean_need(child, "fatigue") < _mean_need(main, "fatigue")
