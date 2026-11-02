"""An EffectProgram marker's guard in the Rust dispatch loop: the Python dispatcher's outcome.

`EventDispatcher.dispatch_one` asks `effect_event_guard_or_suppress` before an event's handler. The
loop in rules/dispatch.rs asks it in Rust: a false guard is a suppressed event, a guard the contract
turns down or with no target a failed one, each with the Python rule's text (saved as the record's
`detail`); a guard that holds goes on to the handler. Each case runs the same event through the Rust
loop and through the Python step with the Python rule of the guard, and compares all that was saved
(docs/rust-port/lanes/l8-texts-events.md).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.world import WorldState
from sheltersim.events import effects
from sheltersim.events.effects import (
    DEFAULT_PRIMITIVE_REGISTRY,
    EffectProgram,
    GuardExpression,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
)
from sheltersim.events.models import EventKind, EventStatus, ScheduledEvent
from sheltersim.experiments.phases.dispatch import DispatchPhase
from sheltersim.experiments.runner import OfflineSimulationController

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

KEY = "effect_program_v1"


class Sink:
    """What the controller writes to, which these tests don't read."""

    def __getattr__(self, name: str) -> Callable[..., None]:
        return lambda *arguments, **options: None


def _gate() -> Any:
    root = str(Path(__file__).resolve().parents[2] / "benchmarks")
    if root not in sys.path:
        sys.path.insert(0, root)
    import gate_scenarios

    return gate_scenarios


@pytest.fixture(scope="module")
def scenario() -> Any:
    return _gate().generated(evacuees=20, minutes=120)


def _counts() -> dict[str, int]:
    return dict(native.rules("dispatch").stats()[0])


def _saved(controller: OfflineSimulationController) -> dict[str, Any]:
    world = controller.world
    return {
        "records": [item.decode() for item in world.event_log.items_bytes(0, len(world.event_log))],
        "pending": [event.to_dict() for event in world.event_queue.pending()],
        "counters": dict(world.id_counters),
        # Not `canonical_json()`: its check turns down a marker of no scheduled program.
        "world": world._canonical_text(world._canonical_data()),
    }


def _marked(event_type: str, payload: dict[str, Any], guard: Any) -> ScheduledEvent:
    return ScheduledEvent(
        event_id="guarded-1",
        kind=EventKind.INTERVENTION,
        due_minute=0,
        priority=0,
        sequence=900_001,
        event_type=event_type,
        payload=payload,
        source_id="TEST-GUARD",
        causation_id=None,
        provenance={KEY: {"guard": guard, "program_id": "no-such-program"}},
    )


def _program(guard: dict[str, Any]) -> EffectProgram:
    """One facility availability change at minute 0, with this guard."""
    spec = DEFAULT_PRIMITIVE_REGISTRY.get("set_facility_availability")
    action = TimedAction(
        effect_id="guarded",
        op="set_facility_availability",
        target_selector=TargetSelector(kind="facility", target_id="toilet_01"),
        start_minute=0,
        end_minute=30,
        parameters={"working_units": 0},
        units=sorted(spec.units)[0],
        source_id="TEST-GUARD",
        assumption_ids=[],
        policy_dependencies=sorted(spec.required_policies),
        reversible=True,
        guard=GuardExpression.model_validate(guard),
    )
    return EffectProgram(
        program_id="program-guarded",
        narrative="guarded",
        source_text="guarded",
        simulation_start_minute=0,
        effects=[action],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


def _scheduled(guard: dict[str, Any]) -> Callable[[WorldState], str]:
    """The program's start event, scheduled by `schedule_effect_program` in a valid world."""

    def arrange(world: WorldState) -> str:
        schedule_effect_program(world, _program(guard))
        return next(
            event.event_id
            for event in world.event_queue.pending()
            if event.event_type == "effect_program_apply"
        )

    return arrange


def _both_ways(
    scenario: Any,
    event: ScheduledEvent | Callable[[WorldState], str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, dict[str, int], dict[str, int]]:
    """The event's record from the Rust loop; the Python step and guard save the same.

    A crafted marker of no program makes a world `validate()` turns down, which neither the guard
    nor the dispatch loop reads; an event whose guard holds goes to its handler, so it comes from
    a program scheduled as an operator's would be.
    """
    by_kernel = OfflineSimulationController(WorldState.from_scenario(scenario), Sink())
    by_python = OfflineSimulationController(WorldState.from_scenario(scenario), Sink())
    for controller in (by_kernel, by_python):
        if isinstance(event, ScheduledEvent):
            controller.world.schedule_event(event)
            event_id = event.event_id
        else:
            event_id = event(controller.world)
    before = _counts()
    by_kernel._run_dispatch_phase(by_kernel.world, 0)
    after = _counts()
    with monkeypatch.context() as patch:
        patch.setattr(
            effects, "should_apply_effect_guard", effects.should_apply_effect_guard.__wrapped__
        )
        DispatchPhase._run_dispatch_phase(by_python, by_python.world, 0)
    assert _saved(by_kernel) == _saved(by_python)
    return by_kernel.world.event_log.find(event_id), before, after


def _grew(before: dict[str, int], after: dict[str, int], *names: str) -> None:
    for name in names:
        assert after[name] == before[name] + 1, name


FACILITY = {"facility_id": "toilet_01", "working_units": 0}


def test_a_false_guard_suppresses_the_event_in_rust(
    scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = {"operator": "lt", "field": "simulation.minute", "value": 0}
    record, before, after = _both_ways(
        scenario, _marked("effect_program_apply", FACILITY, guard), monkeypatch
    )
    assert (record.status, record.detail) == (
        EventStatus.SUPPRESSED,
        "EffectProgram guard evaluated false",
    )
    _grew(before, after, "guard_suppressed", "events_rust")
    assert after["events_python"] == before["events_python"]


@pytest.mark.parametrize(
    "guard",
    [
        {"operator": "eq"},
        {"operator": "eq", "field": "no.such.field", "value": 1},
        {"operator": "gt", "field": "simulation.minute", "value": "x"},
        {
            "operator": "and",
            "operands": [{"operator": "eq", "field": "simulation.minute", "value": 0}],
        },
        {"operator": "eq", "field": "simulation.minute", "value": 0, "other": 1},
    ],
)
def test_a_guard_the_contract_turns_down_fails_with_its_text(
    scenario: Any, monkeypatch: pytest.MonkeyPatch, guard: dict[str, Any]
) -> None:
    record, before, after = _both_ways(
        scenario, _marked("effect_program_apply", FACILITY, guard), monkeypatch
    )
    assert record.status is EventStatus.FAILED
    assert record.detail.startswith("EffectProgram guard is invalid: ")
    _grew(before, after, "guard_failed", "events_rust")


@pytest.mark.parametrize(
    ("field", "payload", "text"),
    [
        ("target.capacity", {}, "guard field 'target.capacity' has no facility target"),
        (
            "target.working_units",
            {"facility_id": "no-such-facility"},
            "guard field 'target.working_units' has no facility target",
        ),
        (
            "target.queue_length",
            {"facility_id": ""},
            "guard field 'target.queue_length' has no facility target",
        ),
        ("zone.occupancy", {}, "guard zone.occupancy has no known zone target"),
        ("zone.occupancy", {"zone_id": "nowhere"}, "guard zone.occupancy has no known zone target"),
        (
            "zone.occupancy",
            {"target_id": "nowhere"},
            "guard zone.occupancy has no known zone target",
        ),
        (
            "resource.available_quantity",
            {"resource_id": "other"},
            "guard resource.available_quantity has no ledger target",
        ),
        (
            "resource.available_quantity",
            {},
            "guard resource.available_quantity has no ledger target",
        ),
        (
            "staff.available",
            {"staff_id": "nobody"},
            "guard staff.available has no known staff target",
        ),
        ("staff.available", {}, "guard staff.available has no known staff target"),
        ("staff.available", {"staff_id": ""}, "guard staff.available has no known staff target"),
    ],
)
def test_a_guard_without_its_target_fails_with_python_s_text(
    scenario: Any, monkeypatch: pytest.MonkeyPatch, field: str, payload: dict[str, Any], text: str
) -> None:
    guard = {"operator": "gte", "field": field, "value": 0}
    if field == "staff.available":
        guard["operator"], guard["value"] = "eq", True
    record, before, after = _both_ways(
        scenario, _marked("effect_program_apply", payload, guard), monkeypatch
    )
    assert (record.status, record.detail) == (EventStatus.FAILED, text)
    _grew(before, after, "guard_failed", "events_rust")


def test_a_later_operand_is_read_only_after_those_before_it(
    scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`and` stops at a false operand, so the missing target after it is never read."""
    first_false = {
        "operator": "and",
        "operands": [
            {"operator": "lt", "field": "simulation.minute", "value": 0},
            {"operator": "gte", "field": "target.capacity", "value": 0},
        ],
    }
    record, _, _ = _both_ways(
        scenario, _marked("effect_program_apply", {}, first_false), monkeypatch
    )
    assert record.detail == "EffectProgram guard evaluated false"
    first_true = {**first_false, "operator": "or"}
    first_true["operands"] = [
        {"operator": "gte", "field": "target.capacity", "value": 0},
        {"operator": "gte", "field": "simulation.minute", "value": 0},
    ]
    record, _, _ = _both_ways(
        scenario, _marked("effect_program_apply", {}, first_true), monkeypatch
    )
    assert record.detail == "guard field 'target.capacity' has no facility target"


def test_a_guard_that_holds_goes_on_to_the_handler(
    scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = {"operator": "gte", "field": "target.working_units", "value": 1}
    record, before, after = _both_ways(scenario, _scheduled(guard), monkeypatch)
    assert (record.status, record.detail) == (EventStatus.APPLIED, None)
    _grew(before, after, "guard_passed", "others_applied", "events_rust")
    assert after["events_python"] == before["events_python"]


def test_a_false_guard_of_a_scheduled_program_suppresses_its_start(
    scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = {"operator": "gt", "field": "target.working_units", "value": 1}
    record, before, after = _both_ways(scenario, _scheduled(guard), monkeypatch)
    assert (record.status, record.detail) == (
        EventStatus.SUPPRESSED,
        "EffectProgram guard evaluated false",
    )
    _grew(before, after, "guard_suppressed", "events_rust")


def test_an_event_without_a_handler_is_python_s_to_record(
    scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = {"operator": "lt", "field": "simulation.minute", "value": 0}
    record, before, after = _both_ways(scenario, _marked("no_such_event", {}, guard), monkeypatch)
    assert (record.status, record.detail) == (
        EventStatus.FAILED,
        "no handler registered for event type 'no_such_event'",
    )
    _grew(before, after, "events_python")
    assert after["guard_suppressed"] == before["guard_suppressed"]


def test_a_marked_event_of_the_table_is_python_s(
    scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = {"operator": "lt", "field": "simulation.minute", "value": 0}
    record, before, after = _both_ways(
        scenario, _marked("support_request_timeout", {}, guard), monkeypatch
    )
    assert (record.status, record.detail) == (
        EventStatus.SUPPRESSED,
        "EffectProgram guard evaluated false",
    )
    _grew(before, after, "events_python")
    assert after["guard_suppressed"] == before["guard_suppressed"]


def test_a_guard_rust_cannot_read_is_left_to_python(
    scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An int past 64 bits is the contract's to take, and Python's to compare."""
    guard = {"operator": "lt", "field": "simulation.minute", "value": 2**70}
    record, before, after = _both_ways(scenario, _scheduled(guard), monkeypatch)
    assert (record.status, record.detail) == (EventStatus.APPLIED, None)
    _grew(before, after, "guard_refused", "events_python")
