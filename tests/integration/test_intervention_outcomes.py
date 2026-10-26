"""An intervention on a UI-generated Scenario changes what the operator sees.

Each test forks the main line of the shared generated Scenario, applies one
approved EffectProgram to the child Branch, advances both with the TickEngine,
and compares the KPIs the Project screen shows. It also checks the recorded
events, so the change has to come from the domain mechanism, not from a KPI.
"""

from __future__ import annotations

import json
from typing import Any

from sheltersim.core.world import WorldState
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


def _program(
    op: str,
    target: TargetSelector,
    parameters: dict[str, Any],
    *,
    units: str,
    policy_dependencies: list[str],
    end_minute: int | None = None,
) -> EffectProgram:
    return EffectProgram(
        program_id=f"program-{op}",
        narrative=op,
        source_text=op,
        simulation_start_minute=FORK_MINUTE,
        effects=[
            TimedAction(
                effect_id=f"{op}-1",
                op=op,
                target_selector=target,
                start_minute=FORK_MINUTE,
                end_minute=end_minute,
                parameters=parameters,
                units=units,
                source_id="TEST-INTERVENTION-OUTCOME",
                policy_dependencies=policy_dependencies,
                reversible=end_minute is not None,
            )
        ],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


def _main_and_child(
    tmp_path, scenario: ScenarioSpec, program: EffectProgram, *, until: int = COMPARE_MINUTE
) -> tuple[WorldState, WorldState]:
    """Run the main line and an intervention Branch forked from it to the same minute."""
    store = ProjectStore(tmp_path)
    store.create_project("p")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    timeline.advance("main", FORK_MINUTE)
    timeline.fork(
        "main",
        FORK_MINUTE,
        "child",
        program=program,
        approved_program_hash=digest(program.model_dump(mode="json")),
    )
    # Checkpoint cadence changes only what is saved, not the simulation; saving
    # every 30 minutes would double the run time of these tests.
    child = timeline.advance("child", until, checkpoint_interval=until).world
    main = timeline.advance("main", until, checkpoint_interval=until).world
    return main, child


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


def test_added_staff_shorten_the_wait_caused_by_busy_staff(tmp_path, generated_scenario) -> None:
    program = _program(
        "add_staff",
        TargetSelector(kind="zone", target_id="staff"),
        {"count": 2},
        units="staff_member",
        policy_dependencies=["staff_operations"],
    )

    main, child = _main_and_child(tmp_path, generated_scenario, program)

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


def test_a_toilet_outage_leaves_toilet_needs_unmet(tmp_path, generated_scenario) -> None:
    program = _program(
        "facility_outage",
        TargetSelector(kind="facility", target_id="toilet_01"),
        {"units": 1},
        units="facility_unit",
        policy_dependencies=["facility_capacity"],
        end_minute=OUTAGE_END_MINUTE,
    )
    # A use that started before the outage may still finish inside it.
    first = FORK_MINUTE + generated_scenario.model_configs["toilet_service"]["maximum_minutes"]

    main, child = _main_and_child(tmp_path, generated_scenario, program)

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
        "water_outage",
        TargetSelector(kind="facility", target_id="water_station_main"),
        {"outage": True},
        units="water_supply",
        policy_dependencies=["operational_events_v1", "resource_service_v1"],
        end_minute=OUTAGE_END_MINUTE,
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
        "deliver_resource",
        TargetSelector(kind="resource", target_id="water_ml"),
        {"warehouse_id": "main_warehouse", "resource_id": "water_ml", "quantity": 100_000},
        units="water_ml",
        policy_dependencies=["resource_ledger_v1"],
    )

    main, child = _main_and_child(tmp_path, scenario, program)

    assert _applied(child, "resource_delivery", resource_id="water_ml", quantity=100_000)
    assert _kpis(main)["water_ml_remaining"] == 0
    assert _kpis(child)["water_ml_remaining"] > 0
    assert len(_applied(child, "resource_service_complete", resource_id="water_ml")) > len(
        _applied(main, "resource_service_complete", resource_id="water_ml")
    )
    assert _kpis(child)["hydration_need_mean"] < _kpis(main)["hydration_need_mean"]
