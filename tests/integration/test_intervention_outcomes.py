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

from sheltersim.core.world import WorldState
from sheltersim.domain.staff_dynamics import staff_dynamics_on_shift
from sheltersim.events.effects import EffectProgram, SupportMode, TargetSelector, TimedAction
from sheltersim.events.models import EventLogRecord, EventStatus
from sheltersim.experiments.branch import BranchRuntime
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


def _program(*effects: TimedAction) -> EffectProgram:
    return EffectProgram(
        program_id="program-" + "-".join(effect.op for effect in effects),
        narrative=", ".join(effect.op for effect in effects),
        source_text=", ".join(effect.op for effect in effects),
        simulation_start_minute=min(effect.start_minute for effect in effects),
        effects=list(effects),
        assumptions=[],
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


def test_repairing_a_reported_broken_toilet_shortens_toilet_waits(
    tmp_path, generated_scenario_with
) -> None:
    # The operator reported toilet_01 out of order until minute 300.
    scenario = generated_scenario_with(
        operational_conditions={
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
    )
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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#74: waiting for an absent worker is counted under no wait reason",
)
def test_a_staff_absence_shows_in_the_support_wait_reasons(staff_absence) -> None:
    main, child = staff_absence
    reasons = (
        "support_wait_staff_busy_request_minutes",
        "support_wait_staff_off_shift_request_minutes",
        "support_no_qualified_worker_request_minutes",
    )

    assert sum(_kpis(child)[name] for name in reasons) > sum(_kpis(main)[name] for name in reasons)


@pytest.mark.xfail(
    strict=True,
    raises=AttributeError,
    reason="#72: set_staff_role fails while the program is scheduled",
)
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


@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason="#76: a delayed delivery stops the simulation when it arrives",
)
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
