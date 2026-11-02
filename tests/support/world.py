"""Parts of WorldState, events and validation for tests of one mechanism."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from support.paths import FAILURE_DAY3


@pytest.fixture
def count_validations(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], list[int]]:
    """Record the id of every object a pydantic model validates from now on.

    For tests of parsed-config caches; monkeypatch.undo() stops the counting.
    """

    def count(model: Any) -> list[int]:
        calls: list[int] = []
        original = model.model_validate.__func__

        def validate(cls, value, *args, **kwargs):
            calls.append(id(value))
            return original(cls, value, *args, **kwargs)

        monkeypatch.setattr(model, "model_validate", classmethod(validate))
        return calls

    return count


def _stress_delta(agent_id: str, minute: int, stress: float) -> Any:
    from sheltersim.policies.psychology import AgentStateDelta

    return AgentStateDelta(
        agent_id=agent_id,
        minute_index=minute,
        toilet_urgency=0.0,
        hunger=0.0,
        fatigue=0.0,
        stress=stress,
        trust=0.5,
    )


@pytest.fixture
def stress_delta() -> Callable[[str, int, float], Any]:
    """Build a psychology update that sets only stress (needs 0, trust 0.5)."""
    return _stress_delta


def _make_event(
    event_id: str,
    *,
    due_minute: int = 0,
    priority: int = 0,
    sequence: int = 0,
    event_type: str = "record",
    payload: dict[str, object] | None = None,
    kind: Any = None,
) -> Any:
    from sheltersim.events.models import EventKind, ScheduledEvent

    return ScheduledEvent(
        event_id=event_id,
        kind=EventKind.EXOGENOUS if kind is None else kind,
        due_minute=due_minute,
        priority=priority,
        sequence=sequence,
        event_type=event_type,
        payload=payload or {},
    )


@pytest.fixture
def make_event() -> Callable[..., Any]:
    """Build a ScheduledEvent: exogenous "record", due at minute 0, priority and sequence 0."""
    return _make_event


class _RecordHandler:
    def __init__(self) -> None:
        self.applied: list[str] = []

    def apply(self, world: Any, event: Any) -> None:
        self.applied.append(event.event_id)


@pytest.fixture
def record_handler() -> type[_RecordHandler]:
    """Make an event handler that changes nothing and lists the ids it applies, in order."""
    return _RecordHandler


@pytest.fixture
def live_checkpoint_world() -> Any:
    """failure_day3 at minute 0 with every kind of runtime state a Checkpoint must keep.

    Two people request toilet_1 (one served, one queued), A003 asks staff for
    support, the model and metric state hold values, and one event is realized
    while the Scenario's events are still pending.
    """
    from sheltersim.core.rng import CounterRNG
    from sheltersim.core.world import WorldState
    from sheltersim.domain.facilities import ToiletServiceManager
    from sheltersim.domain.staff import StaffService
    from sheltersim.events.handlers import EventDispatcher
    from sheltersim.events.models import EventKind, ScheduledEvent
    from sheltersim.experiments.scenario import ScenarioSpec

    world = WorldState.from_scenario(ScenarioSpec.load(FAILURE_DAY3))
    toilet = ToiletServiceManager(world)
    toilet.request("A001", "toilet_1")
    toilet.request("A002", "toilet_1")
    toilet.arbitrate(0, CounterRNG(world.seed))
    staff = StaffService(world)
    staff.request("A003")
    staff.assign_pending(0)
    world.model_state = {"behavior-model": {"adaptive_state": [0.25, 0.75]}}
    world.metric_state["model_accumulator"] = 12.0

    audit_event = ScheduledEvent(
        event_id="checkpoint-audit",
        kind=EventKind.EXOGENOUS,
        due_minute=0,
        priority=0,
        sequence=world.allocate_event_sequence(),
        event_type="unknown_checkpoint_test_event",
    )
    world.schedule_event(audit_event)
    EventDispatcher().dispatch_due(world, 0)
    world.validate()
    assert any(task.kind == "toilet_service" for task in world.active_tasks.values())
    assert any(task.kind == "toilet_queue" for task in world.active_tasks.values())
    assert any(task.kind == "staff_support" for task in world.active_tasks.values())
    assert world.event_queue.pending()
    assert world.event_log.records
    return world


# The attribute an outcome a kernel made carries (native/src/core/errors.rs).
NATIVE_OUTCOME = "_sheltersim_native_outcome"


def kernel_says_python_s_text(handler: Any, world: Any, event: Any) -> tuple[str, str]:
    """The handler's kernel raises its Python rule's outcome itself, with the same class and text.

    `handler.apply` runs the kernel first; the outcome must be the kernel's own (not a refusal the
    Python rule then worded), the world must be as it was, and the Python rule on a copy made
    before must raise the same class with the same text. The class name and text, for the caller.
    (docs/rust-port/lanes/l8-texts-events.md)
    """
    import copy

    from sheltersim.events.models import EventApplicationError, EventSuppressed

    def text(state: Any) -> str:
        return state._canonical_text(state._canonical_data())

    twin = copy.deepcopy(world)
    before = text(world)
    with pytest.raises((EventSuppressed, EventApplicationError)) as made:
        handler.apply(world, event)
    assert getattr(made.value, NATIVE_OUTCOME, False), f"the kernel refused: {made.value}"
    assert text(world) == before
    with pytest.raises((EventSuppressed, EventApplicationError)) as said:
        type(handler).apply.__wrapped__(handler, twin, event)
    assert type(made.value) is type(said.value)
    assert str(made.value) == str(said.value)
    return type(made.value).__name__, str(made.value)
