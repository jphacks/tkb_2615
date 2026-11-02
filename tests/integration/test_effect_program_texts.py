"""An EffectProgram event's suppressed and failed texts are the Python rule's, said by the kernel.

The environment, temporary-space, geometry and structure handlers of events/effects.py
(native/src/rules/effects.rs). These texts are saved: the dispatcher writes them to the event's
record as `detail`. With the kernels, each case checks that the kernel raises the outcome itself
(not a refusal the Python rule then words), that nothing changed, and that the Python rule on a copy
raises the same class with the same text (support/world.py `kernel_says_python_s_text`). Without
them (`SHELTERSIM_NATIVE=0`), the same cases check the Python rule's own text. A handoff needs a
staff operation under way, so its world is run with TickEngine to that point
(docs/rust-port/lanes/l8-texts-events.md).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from support.world import kernel_says_python_s_text

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.events.effects import (
    DEFAULT_PRIMITIVE_REGISTRY,
    EffectProgram,
    EffectProgramEnvironmentHandler,
    EffectProgramGeometryHandler,
    EffectProgramStructureHandler,
    EffectProgramTemporarySpaceHandler,
    EffectProgramValidationError,
    GuardExpression,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
)
from sheltersim.events.models import (
    EventApplicationError,
    EventKind,
    EventSuppressed,
    ScheduledEvent,
)
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _MetricSink

KEY = "effect_program_v1"
START, END = 5, 30
FAILED, SUPPRESSED = "EventApplicationError", "EventSuppressed"


@pytest.fixture
def world(generated_scenario_preview: dict[str, Any]) -> WorldState:
    return WorldState.from_scenario(
        ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    )


def _action(
    op: str,
    kind: str,
    target: str,
    parameters: dict[str, Any],
    *,
    effect_id: str = "effect-1",
    start: int = START,
    end: int | None = END,
    guard: GuardExpression | None = None,
) -> TimedAction:
    spec = DEFAULT_PRIMITIVE_REGISTRY.get(op)
    assert spec is not None
    return TimedAction(
        effect_id=effect_id,
        op=op,
        target_selector=TargetSelector(kind=kind, target_id=target),
        start_minute=start,
        end_minute=end,
        guard=guard,
        parameters=parameters,
        units=sorted(spec.units)[0],
        source_id="TEST-EFFECT-TEXTS",
        assumption_ids=[],
        policy_dependencies=sorted(spec.required_policies),
        reversible=end is not None,
    )


def _program(*actions: TimedAction, program_id: str = "program-1") -> EffectProgram:
    return EffectProgram(
        program_id=program_id,
        narrative="a test program",
        source_text="a test program",
        simulation_start_minute=min(action.start_minute for action in actions),
        effects=list(actions),
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


def _event(world: WorldState, event_type: str, op: str | None = None) -> ScheduledEvent:
    found = [
        item
        for item in world.event_queue.pending()
        if item.event_type == event_type and (op is None or item.payload.get("op") == op)
    ]
    assert len(found) == 1, event_type
    return found[0]


def _changed(event: ScheduledEvent, payload: dict[str, Any] | None = None, **fields: Any) -> Any:
    """The event with payload fields replaced (a value of `...` drops the field)."""
    document = event.to_dict()
    for key, value in (payload or {}).items():
        if value is ...:
            del document["payload"][key]
        else:
            document["payload"][key] = value
    document.update(fields)
    return ScheduledEvent(**document)


def _marker(event: ScheduledEvent, **fields: Any) -> dict[str, Any]:
    provenance = event.to_dict()["provenance"]
    provenance[KEY] = {**provenance[KEY], **fields}
    return provenance


def _says(
    handler: Any, world: WorldState, event: Any, outcome: str, text: str, *, prefix: bool = False
) -> None:
    """The handler's outcome is `outcome` with `text` (or a text starting with it), the kernel's."""
    if native.available():
        said = kernel_says_python_s_text(handler, world, event)
    else:
        with pytest.raises((EventSuppressed, EventApplicationError)) as raised:
            handler.apply(world, event)
        said = (type(raised.value).__name__, str(raised.value))
    assert said[0] == outcome, said
    if prefix:
        assert said[1].startswith(text) and len(said[1]) > len(text), said
    else:
        assert said[1] == text, said


def _a_zone(world: WorldState) -> str:
    return sorted(world.zones)[0]


def _a_toilet(world: WorldState) -> str:
    return sorted(id for id, item in world.facilities.items() if item.kind == "toilet")[0]


# ---- EffectProgramEnvironmentHandler -------------------------------------------------------------


def test_a_zone_environment_event_s_texts_are_python_s(world: WorldState) -> None:
    schedule_effect_program(
        world, _program(_action("set_ambient_noise", "zone", _a_zone(world), {"level": 0.5}))
    )
    start = _event(world, "effect_program_environment")
    expiry = _event(world, "effect_program_environment_expire")
    apply = EffectProgramEnvironmentHandler()
    expire = EffectProgramEnvironmentHandler(expire=True)
    fields = "zone environment event payload fields do not match"
    strings = "zone environment event IDs must be strings"
    policy = "zone environment event has no installed program policy"
    unsupported = "zone environment operator is unsupported"
    minute = "zone environment modifier expiry minute is invalid"
    for handler, event, outcome, text in (
        (apply, _changed(start, {"other": 1}), FAILED, fields),
        (apply, _changed(start, {"end_minute": ...}), FAILED, fields),
        (expire, start, FAILED, fields),
        (apply, _changed(start, {"zone_id": 1}), FAILED, strings),
        (expire, _changed(expiry, {"program_id": None}), FAILED, strings),
        (apply, _changed(start, {"program_id": "no-such-program"}), FAILED, policy),
        (apply, _changed(start, {"effect_id": "no-such-effect"}), FAILED, policy),
        (apply, _changed(start, provenance={}), FAILED, policy),
        (apply, _changed(start, provenance=_marker(start, policy_dependencies=[])), FAILED, policy),
        (
            apply,
            _changed(start, {"modifier_id": "modifier-other"}),
            FAILED,
            "zone environment modifier ID does not match its effect",
        ),
        (expire, expiry, SUPPRESSED, "zone environment modifier is not active"),
        (apply, _changed(start, {"op": "set_noise"}), FAILED, unsupported),
        # A list is frozen to a tuple, which a set can hash.
        (apply, _changed(start, {"op": ["set_lighting"]}), FAILED, unsupported),
        (apply, _changed(start, {"op": None}), FAILED, unsupported),
        (
            apply,
            _changed(start, {"op": "set_lighting"}),
            FAILED,
            "zone environment operator does not match its program",
        ),
        (
            apply,
            _changed(start, {"zone_id": "no-such-zone"}),
            FAILED,
            "zone environment target does not exist",
        ),
        (apply, _changed(start, {"end_minute": 10**30}), FAILED, minute),
        (apply, _changed(start, {"end_minute": True}), FAILED, minute),
        (apply, _changed(start, {"end_minute": float(END)}), FAILED, minute),
        (apply, _changed(start, {"end_minute": START}), FAILED, minute),
        (apply, _changed(start, {"end_minute": world.duration_minutes}), FAILED, minute),
        (
            apply,
            _changed(start, {"end_minute": END - 1}),
            FAILED,
            "zone environment modifier has no unique expiry event",
        ),
        (
            apply,
            _changed(start, provenance=_marker(start, assumption_ids=["a", 1])),
            FAILED,
            "zone environment assumption provenance is invalid",
        ),
        (
            apply,
            _changed(start, provenance=_marker(start, assumption_ids="a")),
            FAILED,
            "zone environment assumption provenance is invalid",
        ),
    ):
        _says(handler, world, event, outcome, text)
    _says(
        apply,
        world,
        _changed(start, {"parameters": {"level": 9.0}}),
        FAILED,
        "zone environment parameters are invalid: ",
        prefix=True,
    )

    apply.apply(world, start)
    _says(apply, world, start, SUPPRESSED, "zone environment modifier is already active")
    _says(
        expire,
        world,
        _changed(expiry, causation_id="another-start"),
        FAILED,
        "zone environment expiry does not match its start",
    )


def test_a_start_the_check_of_the_world_turns_down_is_python_s_text(world: WorldState) -> None:
    """The handler wraps a ValueError of the copy's check: here the record of a start whose
    parameters differ from its event's in the queue."""
    schedule_effect_program(
        world, _program(_action("set_lighting", "zone", _a_zone(world), {"level": 0.5}))
    )
    start = _event(world, "effect_program_environment")
    _says(
        EffectProgramEnvironmentHandler(),
        world,
        _changed(start, {"parameters": {"level": 0.25}}),
        FAILED,
        "zone environment modifier is invalid: ",
        prefix=True,
    )


# ---- EffectProgramTemporarySpaceHandler ----------------------------------------------------------


def test_a_temporary_space_event_s_texts_are_python_s(world: WorldState) -> None:
    zone = _a_zone(world)
    schedule_effect_program(
        world,
        _program(
            _action(
                "add_temporary_space", "zone", zone, {"people_capacity": 5, "sleeping_capacity": 2}
            )
        ),
    )
    start = _event(world, "effect_program_temporary_space")
    expiry = _event(world, "effect_program_temporary_space_expire")
    apply = EffectProgramTemporarySpaceHandler()
    expire = EffectProgramTemporarySpaceHandler(expire=True)
    approved = "temporary-space event does not reference an approved installed effect"
    not_started = "temporary-space effect did not start or does not match its expiry"
    interval = "temporary-space end minute is outside its approved interval"
    for handler, event, outcome, text in (
        (apply, _changed(start, {"program_id": "no-such-program"}), FAILED, approved),
        (apply, _changed(start, {"program_id": 1}), FAILED, approved),
        (apply, _changed(start, {"effect_id": "no-such-effect"}), FAILED, approved),
        (apply, _changed(start, {"modifier_id": "modifier-other"}), FAILED, approved),
        (apply, _changed(start, {"zone_id": "no-such-zone"}), FAILED, approved),
        (apply, _changed(start, {"zone_id": ["a", "b"]}), FAILED, approved),
        (apply, _changed(start, {"zone_id": ...}), FAILED, approved),
        (apply, _changed(start, provenance={}), FAILED, approved),
        (
            apply,
            _changed(start, provenance=_marker(start, policy_dependencies=[])),
            FAILED,
            approved,
        ),
        (
            expire,
            _changed(expiry, {"op": "add_temporary_space"}),
            FAILED,
            "temporary-space expiry payload fields do not match",
        ),
        (expire, expiry, SUPPRESSED, not_started),
        (
            apply,
            _changed(start, {"op": "set_lighting"}),
            FAILED,
            "temporary-space event payload fields do not match",
        ),
        (
            apply,
            _changed(start, {"parameters": ...}),
            FAILED,
            "temporary-space event payload fields do not match",
        ),
        (apply, _changed(start, {"end_minute": 10**30}), FAILED, interval),
        (apply, _changed(start, {"end_minute": START}), FAILED, interval),
        (apply, _changed(start, {"end_minute": True}), FAILED, interval),
        (apply, _changed(start, {"end_minute": None}), FAILED, interval),
    ):
        _says(handler, world, event, outcome, text)
    _says(
        apply,
        world,
        _changed(start, {"parameters": {"people_capacity": 0}}),
        FAILED,
        "temporary-space parameters are invalid: ",
        prefix=True,
    )

    apply.apply(world, start)
    _says(apply, world, start, FAILED, "temporary space ID already exists")
    _says(expire, world, _changed(expiry, causation_id="another-start"), SUPPRESSED, not_started)
    late = _changed(expiry, causation_id=start.event_id, due_minute=END - 1)
    _says(expire, world, late, SUPPRESSED, not_started)


def test_a_temporary_space_whose_guard_is_false_is_suppressed(world: WorldState) -> None:
    schedule_effect_program(
        world,
        _program(
            _action(
                "add_temporary_space",
                "zone",
                _a_zone(world),
                {"people_capacity": 5},
                guard=GuardExpression(operator="gt", field="simulation.minute", value=100_000),
            )
        ),
    )
    start = _event(world, "effect_program_temporary_space")
    _says(
        EffectProgramTemporarySpaceHandler(),
        world,
        start,
        SUPPRESSED,
        "EffectProgram guard evaluated false",
    )


# ---- EffectProgramGeometryHandler ----------------------------------------------------------------


def test_a_geometry_event_s_texts_are_python_s(world: WorldState) -> None:
    edge = sorted(world.zone_edges)[0]
    schedule_effect_program(
        world, _program(_action("set_route_access", "route", edge, {"accessible": True}))
    )
    start = _event(world, "effect_program_geometry")
    expiry = _event(world, "effect_program_geometry_expire")
    apply = EffectProgramGeometryHandler()
    expire = EffectProgramGeometryHandler(expire=True)
    fields = "geometry effect has invalid fields or operator"
    approved = "geometry effect does not reference its approved program"
    for handler, event, outcome, text in (
        (apply, _changed(start, {"other": 1}), FAILED, fields),
        (apply, _changed(start, {"op": "set_lighting"}), FAILED, fields),
        (apply, _changed(start, {"op": ["set_route_access"]}), FAILED, fields),
        (apply, _changed(start, {"program_id": "no-such-program"}), FAILED, approved),
        (apply, _changed(start, {"program_id": 1}), FAILED, approved),
        (apply, _changed(start, {"effect_id": None}), FAILED, approved),
        (apply, _changed(start, {"op": "set_partition"}), FAILED, approved),
        (apply, _changed(start, {"modifier_id": "modifier-other"}), FAILED, approved),
        (expire, expiry, SUPPRESSED, "geometry modifier was not applied or already expired"),
    ):
        _says(handler, world, event, outcome, text)
    # What apply_geometry_change raises is the text, as the Python rule words it.
    for change in ({"target_id": "no-such-route"}, {"parameters": {"accessible": "yes"}}):
        if native.available():
            kernel_says_python_s_text(apply, world, _changed(start, change))
        else:
            with pytest.raises(EventApplicationError):
                apply.apply(world, _changed(start, change))


# ---- EffectProgramStructureHandler: additions, roles, repairs ------------------------------------


@pytest.mark.parametrize(
    ("op", "field", "bound"), [("add_staff", "staff", 100), ("add_toilet", "facilities", 200)]
)
def test_an_addition_s_texts_are_python_s(
    world: WorldState, op: str, field: str, bound: int
) -> None:
    schedule_effect_program(
        world, _program(_action(op, "zone", _a_zone(world), {"count": 1}, end=None))
    )
    # The floor plan has no place for so many toilets; this is about the bound.
    world.model_configs["geometry_overlay"]["schema_version"] = 3
    event = _event(world, "effect_program_structure", op)
    handler = EffectProgramStructureHandler()
    invalid = "structural primitive target or operation is invalid"
    exceed = (
        "adding staff would exceed the 100-staff runtime bound"
        if op == "add_staff"
        else "adding toilets would exceed the 200-facility bound"
    )
    room = bound - len(getattr(world, field))
    for change, text in (
        ({"other": 1}, "structural primitive payload fields do not match"),
        ({"count": ...}, "structural primitive payload fields do not match"),
        ({"program_id": "no-such-program"}, invalid),
        ({"program_id": None}, invalid),
        ({"effect_id": "no-such-effect"}, invalid),
        ({"op": "add_bed"}, invalid),
        ({"op": ["add_staff"]}, invalid),
        ({"count": 0}, invalid),
        ({"count": -(10**30)}, invalid),
        ({"count": True}, invalid),
        ({"count": 1.0}, invalid),
        ({"zone_id": "no-such-zone"}, invalid),
        ({"zone_id": 1}, invalid),
        ({"count": room + 1}, exceed),
        ({"count": 10**30}, exceed),
    ):
        _says(handler, world, _changed(event, change), FAILED, text)
    handler.apply(world, event)
    exists = "generated staff ID" if op == "add_staff" else "generated toilet ID"
    _says(handler, world, event, FAILED, f"{exists} already exists")


def test_a_toilet_without_a_free_point_is_python_s_text(world: WorldState) -> None:
    schedule_effect_program(
        world, _program(_action("add_toilet", "zone", _a_zone(world), {"count": 1}, end=None))
    )
    event = _event(world, "effect_program_structure", "add_toilet")
    many = _changed(event, {"count": 200 - len(world.facilities)})
    _says(
        EffectProgramStructureHandler(),
        world,
        many,
        FAILED,
        "no free floor-plan point for installed facility ",
        prefix=True,
    )


def test_a_role_change_s_texts_are_python_s(world: WorldState) -> None:
    staff_id = sorted(world.staff)[0]
    schedule_effect_program(
        world,
        _program(_action("set_staff_role", "staff", staff_id, {"skills": ["support"]}, end=None)),
    )
    event = _event(world, "effect_program_structure", "set_staff_role")
    handler = EffectProgramStructureHandler()
    target = "staff-role event does not reference an approved staff target"
    for change in (
        {"program_id": "no-such-program"},
        {"program_id": 1},
        {"effect_id": "no-such-effect"},
        {"op": "add_staff"},
        {"staff_id": "no-such-staff"},
        {"staff_id": 1},
    ):
        _says(handler, world, _changed(event, change), FAILED, target)
    _says(
        handler,
        world,
        _changed(event, {"skills": ["same", "same"]}),
        FAILED,
        "staff-role event skills are invalid: ",
        prefix=True,
    )


def test_a_repair_s_texts_are_python_s(world: WorldState) -> None:
    facility = _a_toilet(world)
    world.facilities[facility].working_units = 0
    if "infrastructure_v1" in world.model_state:
        world.model_state["infrastructure_v1"]["base_working_units"][facility] = 0
    capacity = world.facilities[facility].capacity
    parameters = {
        "facility_id": facility,
        "component_id": facility,
        "expected_working_units": 0,
        "restored_working_units": capacity,
    }
    schedule_effect_program(
        world, _program(_action("facility_repair", "facility", facility, parameters, end=None))
    )
    event = _event(world, "effect_program_structure", "facility_repair")
    handler = EffectProgramStructureHandler()
    approved = "facility repair does not reference an approved program"
    for change in (
        {"program_id": "no-such-program"},
        {"effect_id": "no-such-effect"},
        {"op": "facility_outage"},
        {"modifier_id": "modifier-other"},
        {"facility_id": 1},
    ):
        _says(handler, world, _changed(event, change), FAILED, approved)
    for marker in (
        {"op": "add_staff"},
        {"phase": "end"},
        {"program_id": "other"},
        {"effect_id": None},
    ):
        provenance = _marker(event, **marker)
        _says(handler, world, _changed(event, provenance=provenance), FAILED, approved)
    _says(handler, world, _changed(event, event_id="another-event"), FAILED, approved)
    _says(
        handler,
        world,
        _changed(event, {"parameters": {"facility_id": facility}}),
        FAILED,
        "facility repair parameters are invalid: ",
        prefix=True,
    )
    other = sorted(set(world.facilities) - {facility})[0]
    _says(
        handler,
        world,
        _changed(event, {"facility_id": other}),
        FAILED,
        "facility repair target does not match a known facility",
    )
    world.facilities[facility].working_units = capacity
    _says(
        handler,
        world,
        event,
        FAILED,
        "facility repair preconditions no longer match the live facility state",
    )
    world.facilities[facility].working_units = 0
    del world.model_state["shared_operations_v1"]
    _says(handler, world, event, FAILED, "facility repair requires shared receipt state")


# ---- EffectProgramStructureHandler: a handoff ----------------------------------------------------


@pytest.fixture(scope="module")
def handing_off(
    generated_scenario_preview: dict[str, Any],
) -> tuple[WorldState, ScheduledEvent, ScheduledEvent]:
    """A world where a patrol is under way and an idle patroller waits in its zone; the handoff,
    and a role change of the worker on patrol."""
    world = WorldState.from_scenario(
        ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    )
    engine = TickEngine(
        world, phase_handler=OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    )
    operations = world.model_state["staff_operations_v1"]["staff"]
    idle = [id for id, staff in sorted(world.staff.items()) if operations[id]["phase"] == "idle"]
    worker = next(id for id in idle if "support" in world.staff[id].skills)
    # One who does nothing but patrol: sent to the patrol's zone first, it waits there idle.
    replacement = next(id for id in idle if "support" not in world.staff[id].skills)
    zone = sorted({agent.zone_id for agent in world.agents.values()})[0]
    schedule_effect_program(
        world,
        _program(
            _action(
                "set_staff_role",
                "staff",
                worker,
                {"skills": ["patrol", "support"]},
                start=world.minute_index + 300,
                end=None,
            ),
            _action(
                "set_staff_role",
                "staff",
                replacement,
                {"skills": ["patrol"]},
                effect_id="effect-2",
                start=world.minute_index,
                end=None,
            ),
            program_id="roles",
        ),
    )
    engine.step()

    def order(staff_id: str, program_id: str) -> None:
        """Order a patrol at the first minute the worker may take one."""
        for _ in range(240):
            try:
                schedule_effect_program(
                    world,
                    _program(
                        _action(
                            "staff_task_order",
                            "staff",
                            staff_id,
                            {
                                "task_kind": "patrol",
                                "target_zone_id": zone,
                                "reason": "a test patrol",
                            },
                            start=world.minute_index,
                            end=None,
                        ),
                        program_id=program_id,
                    ),
                )
                return
            except EffectProgramValidationError:
                engine.step()
        pytest.fail(f"{staff_id} took no order")

    order(replacement, "send-replacement")
    for _ in range(60):
        engine.step()
        phase = world.model_state["staff_operations_v1"]["staff"][replacement]["phase"]
        if phase == "idle" and world.staff[replacement].zone_id == zone:
            break
    else:
        pytest.fail("the replacement did not come to the zone")
    order(worker, "patrol")
    for _ in range(60):
        engine.step()
        record = world.model_state["staff_operations_v1"]["staff"][worker]
        if record["phase"] == "working" and world.staff[replacement].active_task_id is None:
            break
    else:
        pytest.fail("no patrol came to work")
    schedule_effect_program(
        world,
        _program(
            _action(
                "staff_handoff",
                "staff",
                worker,
                {
                    "staff_id": worker,
                    "replacement_staff_id": replacement,
                    "task_id": record["task_id"],
                    "reason_code": "reassigned",
                },
                start=world.minute_index,
                end=None,
            ),
            program_id="handoff",
        ),
    )
    return (
        world,
        _event(world, "effect_program_structure", "staff_handoff"),
        _event(world, "effect_program_structure", "set_staff_role"),
    )


def test_a_handoff_s_texts_are_python_s(
    handing_off: tuple[WorldState, ScheduledEvent, ScheduledEvent],
) -> None:
    base, event, _ = handing_off
    worker = event.payload["staff_id"]
    parameters = event.to_dict()["payload"]["parameters"]
    replacement = parameters["replacement_staff_id"]
    # Someone not idle at the patrol: the generated workers are all elsewhere or busy.
    other = next(
        id
        for id, staff in sorted(base.staff.items())
        if id not in {worker, replacement} and staff.zone_id != base.staff[replacement].zone_id
    )
    handler = EffectProgramStructureHandler()

    def fresh() -> WorldState:
        return WorldState.from_dict(base.to_dict())

    if native.available():
        # The handoff itself is the Python rule's, receipt and all.
        python_world, kernel_world = fresh(), fresh()
        expected = type(handler).apply.__wrapped__(handler, python_world, event)
        made = native.rules("effects").structure(handler, kernel_world, event)
        assert [item.to_dict() for item in made] == [item.to_dict() for item in expected]
        assert kernel_world.to_dict() == python_world.to_dict()
        assert kernel_world.staff[replacement].active_task_id is not None

    approved = "staff handoff does not reference an approved program"
    for change in (
        {"program_id": "no-such-program"},
        {"op": "staff_task_order_x"},
        {"staff_id": 1},
        {"modifier_id": "modifier-other"},
    ):
        _says(handler, fresh(), _changed(event, change), FAILED, approved)
    _says(handler, fresh(), _changed(event, provenance={}), FAILED, approved)
    _says(
        handler,
        fresh(),
        _changed(event, {"parameters": {"staff_id": worker}}),
        FAILED,
        "staff handoff parameters are invalid: ",
        prefix=True,
    )
    participants = "staff handoff participants or task are invalid"
    for change in (
        {"replacement_staff_id": "no-such-staff"},
        {"replacement_staff_id": None},
        {"task_id": "no-such-task"},
    ):
        changed = _changed(event, {"parameters": {**parameters, **change}})
        _says(handler, fresh(), changed, FAILED, participants)
    _says(
        handler,
        fresh(),
        _changed(event, {"parameters": {**parameters, "replacement_staff_id": other}}),
        FAILED,
        "staff handoff requires a qualified idle replacement already at active work",
    )
    # The replacement called away, through the dispatcher, at this minute (the handoff kept back).
    unavailable = fresh()
    later = _changed(event, due_minute=event.due_minute + 5)
    unavailable.event_queue.replace(event.event_id, later)
    unavailable.event_queue.schedule(
        ScheduledEvent(
            event_id="test-absence",
            kind=EventKind.EXOGENOUS,
            due_minute=unavailable.minute_index,
            priority=0,
            sequence=unavailable.allocate_event_sequence(),
            event_type="staff_unavailable",
            payload={"staff_id": replacement, "until_minute": unavailable.minute_index + 30},
            source_id="SRC-TEST",
            causation_id=None,
            provenance={"source_type": "assumption", "source_id": "SRC-TEST"},
        )
    )
    controller = OfflineSimulationController(unavailable, _MetricSink(MetricsRecorder()))
    TickEngine(unavailable, phase_handler=controller).step()
    status = unavailable.model_state["operational_events_v1"]["staff_status"][replacement]
    assert status["available"] is False
    _says(handler, unavailable, event, FAILED, "staff handoff replacement is unavailable")
    world = fresh()
    del world.model_state["shared_operations_v1"]
    _says(handler, world, event, FAILED, participants)


def test_a_role_change_during_work_is_python_s_text(
    handing_off: tuple[WorldState, ScheduledEvent, ScheduledEvent],
) -> None:
    base, _, role = handing_off
    _says(
        EffectProgramStructureHandler(),
        WorldState.from_dict(base.to_dict()),
        role,
        FAILED,
        "staff role cannot change while work is active",
    )
