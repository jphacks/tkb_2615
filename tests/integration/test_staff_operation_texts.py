"""An operator's staff order, and a worker unavailable mid-task: the Python rules' results in Rust.

`order_staff_operation` and its checks (`validate_staff_operation_order`) run in
rules/staff_order.rs for an EffectProgram's `staff_task_order`: each failed check has the Python
rule's text, raised before any change, and an order that passes writes what the Python rule
writes. The arrival and the completion of a task whose worker became unavailable stop the
operation and then are suppressed, as the Python rules do (docs/rust-port/lanes/l8-texts-events.md).
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest
from support.world import NATIVE_OUTCOME, kernel_says_python_s_text

from sheltersim import native
from sheltersim.core.world import WorldState
from sheltersim.domain.staff_operations import order_staff_operation
from sheltersim.events.effects import (
    DEFAULT_PRIMITIVE_REGISTRY,
    EffectProgram,
    EffectProgramStructureHandler,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
)
from sheltersim.events.handlers import (
    StaffOperationArrivalHandler,
    StaffOperationCompletionHandler,
)
from sheltersim.events.models import EventApplicationError, EventSuppressed

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")


def _gate() -> Any:
    root = str(Path(__file__).resolve().parents[2] / "benchmarks")
    if root not in sys.path:
        sys.path.insert(0, root)
    import gate_scenarios

    return gate_scenarios


@pytest.fixture(scope="module")
def scenario() -> Any:
    """Staff_001 (zone `staff`) has support, information and intake; the others meals and water."""
    return _gate().generated(evacuees=20, minutes=120)


@pytest.fixture
def world(scenario: Any) -> WorldState:
    return WorldState.from_scenario(scenario)


def _copy(world: WorldState) -> WorldState:
    """A copy, as the test changed it: `from_dict` would check what a test took away."""
    return copy.deepcopy(world)


def _text(world: WorldState) -> str:
    return world._canonical_text(world._canonical_data())


def _order(**changes: Any) -> dict[str, Any]:
    return {
        "staff_id": "staff_001",
        "task_kind": "patrol",
        "target_zone_id": "sleeping",
        "target_id": None,
        "reason": "a test order",
        "causation_event_id": "order-1",
        "source_id": None,
        **changes,
    }


def _ordered_both_ways(world: WorldState, order: dict[str, Any]) -> str | None:
    """Rust and Python on copies: the same text before any change, or the same task and world."""
    by_python, by_rust = _copy(world), _copy(world)
    before = _text(by_rust)
    try:
        expected: Any = order_staff_operation(by_python, **order)
    except EventApplicationError as said:
        with pytest.raises(EventApplicationError) as made:
            native.rules("staff_order").order(by_rust, **order)
        assert getattr(made.value, NATIVE_OUTCOME, False), f"not Rust's own: {made.value}"
        assert str(made.value) == str(said)
        assert _text(by_rust) == before
        return str(said)
    assert native.rules("staff_order").order(by_rust, **order) == expected
    assert _text(by_rust) == _text(by_python)
    return None


# ---- The checks, in the Python rule's order ---------------------------------------------------


def test_an_order_without_its_state_is_python_s_text(world: WorldState) -> None:
    del world.model_state["shared_operations_v1"]
    assert (
        _ordered_both_ways(world, _order())
        == "staff task order requires staff and shared operation state"
    )


def test_an_order_for_unknown_staff_is_python_s_text(world: WorldState) -> None:
    assert (
        _ordered_both_ways(world, _order(staff_id="nobody"))
        == "staff task order references unknown staff"
    )


def test_an_order_for_a_busy_or_unavailable_worker_is_python_s_text(world: WorldState) -> None:
    text = "staff task order requires an idle, available, on-shift worker"
    busy = _copy(world)
    order_staff_operation(busy, **_order())
    assert _ordered_both_ways(busy, _order(causation_event_id="order-2")) == text
    away = _copy(world)
    away.model_state["operational_events_v1"]["staff_status"]["staff_001"]["available"] = False
    assert _ordered_both_ways(away, _order()) == text


def test_an_order_of_an_unknown_task_kind_is_python_s_text(world: WorldState) -> None:
    assert (
        _ordered_both_ways(world, _order(task_kind="juggling"))
        == "staff task order uses an unregistered task kind"
    )


def test_an_order_for_a_worker_without_the_skill_names_who_has_it(world: WorldState) -> None:
    said = _ordered_both_ways(world, _order(staff_id="staff_002"))
    assert said is not None and said.startswith("staff_lacks_task_skill: staff_002")
    assert "staff_001" in said


@pytest.mark.parametrize("kind", ["cleaning", "maintenance"])
def test_an_order_no_one_is_qualified_for_says_so(world: WorldState, kind: str) -> None:
    target = "toilet_01" if kind == "maintenance" else None
    said = _ordered_both_ways(
        world, _order(task_kind=kind, target_zone_id="toilet", target_id=target)
    )
    assert said is not None and said.startswith("no_qualified_staff: ")


def test_an_order_to_an_unknown_zone_is_python_s_text(world: WorldState) -> None:
    assert (
        _ordered_both_ways(world, _order(target_zone_id="nowhere"))
        == "staff task order references an unknown target zone"
    )


def _repairer(world: WorldState) -> WorldState:
    world.staff["staff_002"].skills = ("repair", *world.staff["staff_002"].skills)
    return world


@pytest.mark.parametrize(
    ("target", "zone"), [(None, "toilet"), ("no-such-facility", "toilet"), ("toilet_01", "food")]
)
def test_a_maintenance_order_off_its_facility_is_python_s_text(
    world: WorldState, target: str | None, zone: str
) -> None:
    order = _order(
        staff_id="staff_002", task_kind="maintenance", target_zone_id=zone, target_id=target
    )
    assert (
        _ordered_both_ways(_repairer(world), order)
        == "maintenance order must target a facility in its selected zone"
    )


def test_a_maintenance_order_with_nothing_to_repair_is_python_s_text(world: WorldState) -> None:
    order = _order(
        staff_id="staff_002",
        task_kind="maintenance",
        target_zone_id="toilet",
        target_id="toilet_01",
    )
    assert (
        _ordered_both_ways(_repairer(world), order)
        == "maintenance order has no currently repairable facility unit"
    )


def test_a_maintenance_order_is_the_same_in_both(world: WorldState) -> None:
    world = _repairer(world)
    world.facilities["toilet_01"].working_units = 0
    if "infrastructure_v1" in world.model_state:
        world.model_state["infrastructure_v1"]["base_working_units"]["toilet_01"] = 0
    order = _order(
        staff_id="staff_002",
        task_kind="maintenance",
        target_zone_id="toilet",
        target_id="toilet_01",
    )
    assert _ordered_both_ways(world, order) is None


def test_only_a_maintenance_order_takes_a_facility(world: WorldState) -> None:
    assert (
        _ordered_both_ways(world, _order(target_id="toilet_01"))
        == "only maintenance orders accept a target facility"
    )


def test_a_support_order_where_no_one_is_is_python_s_text(world: WorldState) -> None:
    assert (
        _ordered_both_ways(world, _order(target_zone_id="quiet"))
        == "staff support order requires residents in its target zone"
    )


def test_outreach_without_the_body_state_is_python_s_text(world: WorldState) -> None:
    del world.model_state["body_state_v1"]
    assert (
        _ordered_both_ways(world, _order(task_kind="information_outreach"))
        == "information outreach requires the body-state policy"
    )


def test_intake_without_arrivals_is_python_s_text(world: WorldState) -> None:
    assert (
        _ordered_both_ways(world, _order(task_kind="intake", target_zone_id="intake"))
        == "intake order requires pending arrivals"
    )


def test_an_order_with_no_route_is_python_s_text(world: WorldState) -> None:
    world.zone_edges["route_corridor_sleeping"].closed = True
    assert _ordered_both_ways(world, _order()) == "staff task order has no reachable route"


def test_an_order_past_the_run_is_python_s_text(world: WorldState) -> None:
    world.duration_minutes = world.minute_index + 3
    assert (
        _ordered_both_ways(world, _order())
        == "staff task order would finish beyond the Run boundary"
    )


# ---- What an order writes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "order",
    [
        _order(),
        _order(source_id="SRC-ORDER", reason="さらに巡回"),
        _order(task_kind="information_outreach"),
    ],
)
def test_an_order_writes_what_the_python_rule_writes(
    world: WorldState, order: dict[str, Any]
) -> None:
    assert _ordered_both_ways(world, order) is None


def test_an_order_without_a_walk_writes_what_the_python_rule_writes(world: WorldState) -> None:
    world.staff["staff_001"].zone_id = "sleeping"
    assert _ordered_both_ways(world, _order()) is None


def test_a_policy_selected_formula_gives_the_order_its_source(world: WorldState) -> None:
    world.model_configs.setdefault("policy_registry", {})["staff_operations"] = (
        "staff-travel-first-v1"
    )
    assert _ordered_both_ways(world, _order()) is None


# ---- Through the EffectProgram's handler --------------------------------------------------------


def _ordered_program(world: WorldState) -> Any:
    spec = DEFAULT_PRIMITIVE_REGISTRY.get("staff_task_order")
    action = TimedAction(
        effect_id="effect-1",
        op="staff_task_order",
        target_selector=TargetSelector(kind="staff", target_id="staff_001"),
        start_minute=world.minute_index,
        end_minute=None,
        parameters={"task_kind": "patrol", "target_zone_id": "sleeping", "reason": "a test order"},
        units=sorted(spec.units)[0],
        source_id="TEST-STAFF-ORDER",
        assumption_ids=[],
        policy_dependencies=sorted(spec.required_policies),
        reversible=False,
    )
    schedule_effect_program(
        world,
        EffectProgram(
            program_id="program-1",
            narrative="a test order",
            source_text="a test order",
            simulation_start_minute=world.minute_index,
            effects=[action],
            assumptions=[],
            support_mode=SupportMode.NATIVE,
            compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
        ),
    )
    return next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "effect_program_structure"
    )


def test_an_order_that_went_stale_fails_in_the_handler_with_python_s_text(
    world: WorldState,
) -> None:
    event = _ordered_program(world)
    world.duration_minutes = world.minute_index + 3
    said = kernel_says_python_s_text(EffectProgramStructureHandler(), world, event)
    assert said == (
        "EventApplicationError",
        "staff task order would finish beyond the Run boundary",
    )


def test_an_order_through_the_handler_is_the_same_in_both(world: WorldState) -> None:
    event = _ordered_program(world)
    by_python, by_rust = _copy(world), _copy(world)
    handler = EffectProgramStructureHandler()
    type(handler).apply.__wrapped__(handler, by_python, event)
    handler.apply(by_rust, event)
    assert _text(by_rust) == _text(by_python)


# ---- A worker who became unavailable ------------------------------------------------------------


def _canonical(world: WorldState) -> str:
    return world._canonical_text(world._canonical_data())


def _pending(world: WorldState, event_type: str) -> Any:
    return next(event for event in world.event_queue.pending() if event.event_type == event_type)


def _stops_and_suppresses(handler: Any, world: WorldState, event: Any, text: str) -> None:
    """Rust's own outcome, after the stop the Python rule makes on a copy made before."""
    twin = copy.deepcopy(world)
    before = _canonical(world)
    with pytest.raises(EventSuppressed) as made:
        handler.apply(world, event)
    assert getattr(made.value, NATIVE_OUTCOME, False), f"the kernel refused: {made.value}"
    assert str(made.value) == text
    with pytest.raises(EventSuppressed) as said:
        type(handler).apply.__wrapped__(handler, twin, event)
    assert str(said.value) == text
    assert _canonical(world) != before
    assert _canonical(world) == _canonical(twin)
    record = world.model_state["staff_operations_v1"]["staff"]["staff_001"]
    assert record["phase"] == "idle"
    assert world.staff["staff_001"].active_task_id is None


def _away(world: WorldState) -> None:
    world.model_state["operational_events_v1"]["staff_status"]["staff_001"]["available"] = False


def test_an_arrival_of_a_worker_who_became_unavailable_stops_the_task(world: WorldState) -> None:
    order_staff_operation(world, **_order())
    event = _pending(world, "staff_operation_arrive")
    _away(world)
    _stops_and_suppresses(
        StaffOperationArrivalHandler(),
        world,
        event,
        "staff became unavailable before reaching the assigned task",
    )
    # Its completion left the queue with the task.
    assert not any(
        item.event_type == "staff_operation_complete" for item in world.event_queue.pending()
    )


def test_a_completion_of_a_worker_who_became_unavailable_stops_the_task(world: WorldState) -> None:
    order_staff_operation(world, **_order())
    StaffOperationArrivalHandler().apply(world, _pending(world, "staff_operation_arrive"))
    event = _pending(world, "staff_operation_complete")
    _away(world)
    _stops_and_suppresses(
        StaffOperationCompletionHandler(),
        world,
        event,
        "staff became unavailable before completing the assigned task",
    )
