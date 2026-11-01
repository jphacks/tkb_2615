"""The rules of events/effects.py in Rust give what the Python rules give, and refuse the rest.

native/src/rules/effects.rs (docs/rust-port/lanes/m6-effects.md). An effect's events are rare, and
7 simulated days have none, so this file and the gate are all that compares them. Each handler runs
on two copies of one world, and the whole documents must be the same text of JSON. Anything but a
success must leave the world as it was and say Unsupported, so the Python rule words the error.
"""

from __future__ import annotations

import copy
import json
import random
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.state_mirror import rust_validated
from sheltersim.core.world import WorldState
from sheltersim.domain import geometry_overlay
from sheltersim.events import effects
from sheltersim.events.effects import (
    DEFAULT_PRIMITIVE_REGISTRY,
    EFFECT_EVENT_TYPES,
    EffectProgram,
    EffectProgramEnvironmentHandler,
    EffectProgramFacilityHandler,
    EffectProgramStructureHandler,
    EffectProgramTemporarySpaceHandler,
    GuardExpression,
    SupportMode,
    TargetSelector,
    TimedAction,
    has_active_facility_modifier,
    has_pending_facility_modifier,
    schedule_effect_program,
    should_apply_effect_guard,
    validate_effect_program_state,
)
from sheltersim.events.models import EventApplicationError, ScheduledEvent
from sheltersim.experiments.scenario import ScenarioSpec

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

KEY = "effect_program_v1"
START, END = 5, 30


@pytest.fixture(scope="module")
def rules() -> Any:
    return native.rules("effects")


@pytest.fixture
def world(generated_scenario_preview) -> WorldState:
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
        source_id="TEST-EFFECT-KERNELS",
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


def _event(world: WorldState, event_type: str) -> ScheduledEvent:
    found = [item for item in world.event_queue.pending() if item.event_type == event_type]
    assert len(found) == 1, event_type
    return found[0]


def _copy(world: WorldState) -> WorldState:
    return WorldState.from_dict(world.to_dict())


def _text(world: WorldState) -> str:
    return json.dumps(world.to_dict(), ensure_ascii=False)


def _same_in_both(
    kernel: Any, handler: Any, world: WorldState, event: ScheduledEvent
) -> WorldState:
    """The kernel's result on a copy, after the Python rule's on another gave the same."""
    python_world = _copy(world)
    kernel_world = _copy(world)
    expected = type(handler).apply.__wrapped__(handler, python_world, event)
    result = kernel(handler, kernel_world, event)
    assert [item.to_dict() for item in result or ()] == [item.to_dict() for item in expected or ()]
    assert _text(kernel_world) == _text(python_world)
    if KEY in kernel_world.model_state:
        assert kernel_world.model_state._mirrors[KEY].verify()
    return kernel_world


def _refused(kernel: Any, handler: Any, world: WorldState, event: ScheduledEvent) -> None:
    before = _text(world)
    with pytest.raises(native.Unsupported):
        kernel(handler, world, event)
    assert _text(world) == before


def _a_facility(world: WorldState, kind: str = "toilet") -> str:
    return sorted(id for id, item in world.facilities.items() if item.kind == kind)[0]


def _a_zone(world: WorldState) -> str:
    return sorted(world.zones)[0]


def test_the_kernel_is_the_one_that_runs(world: WorldState, rules: Any) -> None:
    """A kernel can say Unsupported on every call while everything else passes."""
    facility = _a_facility(world)
    schedule_effect_program(
        world,
        _program(_action("set_facility_availability", "facility", facility, {"working_units": 0})),
    )
    before, _ = rules.stats()
    EffectProgramFacilityHandler().apply(world, _event(world, "effect_program_apply"))
    after, _ = rules.stats()
    assert after["facility"] == before.get("facility", 0) + 1
    assert world.facilities[facility].working_units == 0


# ---- The handlers ----------------------------------------------------------------------------


def test_a_facility_modifier_starts_stacks_and_expires(world: WorldState, rules: Any) -> None:
    facility = _a_facility(world)
    schedule_effect_program(
        world,
        _program(
            _action(
                "set_facility_availability",
                "facility",
                facility,
                {"working_units": 0},
                effect_id="first",
                end=20,
            ),
            _action(
                "set_facility_availability",
                "facility",
                facility,
                {"working_units": 0},
                effect_id="second",
                end=25,
            ),
        ),
    )
    starts = [e for e in world.event_queue.pending() if e.event_type == "effect_program_apply"]
    expiries = [e for e in world.event_queue.pending() if e.event_type == "effect_program_expire"]
    apply, expire = EffectProgramFacilityHandler(), EffectProgramFacilityHandler(expire=True)
    # Expiring what did not start is suppressed, by the Python rule.
    _refused(rules.facility, expire, world, expiries[0])
    state = _same_in_both(rules.facility, apply, world, starts[0])
    state = _same_in_both(rules.facility, apply, state, starts[1])
    # A modifier already active is suppressed, by the Python rule.
    _refused(rules.facility, apply, state, starts[1])
    assert len(state.model_state[KEY]["active_facility_modifiers"][facility]["modifiers"]) == 2
    assert has_active_facility_modifier(state, facility)
    state = _same_in_both(rules.facility, expire, state, expiries[0])
    assert len(state.model_state[KEY]["active_facility_modifiers"][facility]["modifiers"]) == 1
    state = _same_in_both(rules.facility, expire, state, expiries[1])
    assert facility not in state.model_state[KEY]["active_facility_modifiers"]
    assert not has_active_facility_modifier(state, facility)


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("working_units", True),
        ("working_units", 1.0),
        ("working_units", "0"),
        ("working_units", 10_000),
        ("working_units", -1),
        ("facility_id", "no-such-facility"),
        ("facility_id", 1),
        ("program_id", "no-such-program"),
        ("effect_id", "no-such-effect"),
        ("modifier_id", 1),
        ("unknown_field", 1),
    ],
)
def test_a_facility_event_the_rule_is_unsure_of_goes_to_python(
    world: WorldState, rules: Any, change: str, value: Any
) -> None:
    facility = _a_facility(world)
    schedule_effect_program(
        world,
        _program(_action("set_facility_availability", "facility", facility, {"working_units": 0})),
    )
    event = _event(world, "effect_program_apply")
    document = event.to_dict()
    document["payload"][change] = value
    _refused(rules.facility, EffectProgramFacilityHandler(), world, ScheduledEvent(**document))


ZONE_OPERATORS = {
    "set_ambient_noise": {"level": 1.0},
    "set_lighting": {"level": 1.0},
    "set_ventilation": {"effectiveness": 0.0},
    "set_privacy": {"level": 0.0},
    "set_temperature": {"temperature_c": 35.0},
    "modify_demand_rate": {
        "action": "rest",
        "probability_multiplier": 2.0,
        "duration_multiplier": 2.0,
    },
}


@pytest.mark.parametrize("op", sorted(ZONE_OPERATORS))
def test_a_zone_condition_starts_and_expires(world: WorldState, rules: Any, op: str) -> None:
    schedule_effect_program(
        world, _program(_action(op, "zone", _a_zone(world), ZONE_OPERATORS[op]))
    )
    start = _event(world, "effect_program_environment")
    expiry = _event(world, "effect_program_environment_expire")
    apply = EffectProgramEnvironmentHandler()
    expire = EffectProgramEnvironmentHandler(expire=True)
    _refused(rules.environment, expire, world, expiry)
    state = _same_in_both(rules.environment, apply, world, start)
    assert state.model_state["zone_environment_v1"]["modifiers"]
    _refused(rules.environment, apply, state, start)
    state = _same_in_both(rules.environment, expire, state, expiry)
    assert not state.model_state["zone_environment_v1"]["modifiers"]


def test_zone_parameters_stay_python_s_to_refuse(world: WorldState, rules: Any) -> None:
    schedule_effect_program(
        world, _program(_action("set_lighting", "zone", _a_zone(world), {"level": 1.0}))
    )
    document = _event(world, "effect_program_environment").to_dict()
    document["payload"]["parameters"] = {"level": 9.0}
    event = ScheduledEvent(**document)
    _refused(rules.environment, EffectProgramEnvironmentHandler(), world, event)


def test_a_temporary_space_starts_and_expires(world: WorldState, rules: Any) -> None:
    schedule_effect_program(
        world,
        _program(
            _action(
                "add_temporary_space",
                "zone",
                _a_zone(world),
                {"people_capacity": 5, "sleeping_capacity": 2},
            )
        ),
    )
    start = _event(world, "effect_program_temporary_space")
    expiry = _event(world, "effect_program_temporary_space_expire")
    apply = EffectProgramTemporarySpaceHandler()
    expire = EffectProgramTemporarySpaceHandler(expire=True)
    _refused(rules.temporary_space, expire, world, expiry)
    state = _same_in_both(rules.temporary_space, apply, world, start)
    document = expiry.to_dict()
    document["causation_id"] = start.event_id
    state = _same_in_both(rules.temporary_space, expire, state, ScheduledEvent(**document))
    space = next(iter(state.model_state["temporary_spaces_v1"]["spaces"].values()))
    assert space["active"] is False


def _structure_event(world: WorldState, op: str) -> ScheduledEvent:
    found = [
        item
        for item in world.event_queue.pending()
        if item.event_type == "effect_program_structure" and item.payload.get("op") == op
    ]
    assert len(found) == 1
    return found[0]


@pytest.mark.parametrize(
    ("op", "kind", "parameters"),
    [
        ("add_staff", "staff_member", {"count": 2}),
        ("add_toilet", "toilet_stall", {"count": 2}),
    ],
)
def test_a_new_member_or_toilet_is_the_same(
    world: WorldState, rules: Any, op: str, kind: str, parameters: dict[str, Any]
) -> None:
    schedule_effect_program(
        world, _program(_action(op, "zone", _a_zone(world), parameters, end=None))
    )
    state = _same_in_both(
        rules.structure, EffectProgramStructureHandler(), world, _structure_event(world, op)
    )
    if op == "add_staff":
        assert len(state.staff) == len(world.staff) + 2
    else:
        assert len(state.facilities) == len(world.facilities) + 2


def test_past_the_bound_of_members_goes_to_python(world: WorldState, rules: Any) -> None:
    schedule_effect_program(
        world, _program(_action("add_staff", "zone", _a_zone(world), {"count": 2}, end=None))
    )
    document = _structure_event(world, "add_staff").to_dict()
    document["payload"]["count"] = 1000
    event = ScheduledEvent(**document)
    _refused(rules.structure, EffectProgramStructureHandler(), world, event)


@pytest.mark.parametrize(
    ("op", "field", "bound"), [("add_staff", "staff", 100), ("add_toilet", "facilities", 200)]
)
def test_the_bound_of_an_addition_is_the_python_rule_s(
    world: WorldState,
    rules: Any,
    op: str,
    field: str,
    bound: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule_effect_program(
        world, _program(_action(op, "zone", _a_zone(world), {"count": 1}, end=None))
    )
    # The floor plan has no place for so many toilets; this is about the bound.
    for module in (effects, geometry_overlay):
        monkeypatch.setattr(module, "installed_facility_placement_enabled", lambda _: False)
    document = _structure_event(world, op).to_dict()
    room = bound - len(getattr(world, field))
    handler = EffectProgramStructureHandler()
    document["payload"]["count"] = room
    _same_in_both(rules.structure, handler, world, ScheduledEvent(**document))
    document["payload"]["count"] = room + 1
    _refused(rules.structure, handler, world, ScheduledEvent(**document))
    with pytest.raises(EventApplicationError):
        type(handler).apply.__wrapped__(handler, _copy(world), ScheduledEvent(**document))


def test_a_role_change_is_the_same_and_its_contract_stays_python_s(
    world: WorldState, rules: Any
) -> None:
    staff_id = sorted(world.staff)[0]
    schedule_effect_program(
        world,
        _program(
            _action("set_staff_role", "staff", staff_id, {"skills": ["zeta", "alpha"]}, end=None)
        ),
    )
    event = _structure_event(world, "set_staff_role")
    handler = EffectProgramStructureHandler()
    state = _same_in_both(rules.structure, handler, world, event)
    assert state.staff[staff_id].skills == ("alpha", "zeta")
    document = event.to_dict()
    document["payload"]["skills"] = ["same", "same"]
    _refused(rules.structure, handler, world, ScheduledEvent(**document))


def test_a_repair_is_the_same_and_checks_what_it_expects(world: WorldState, rules: Any) -> None:
    facility = _a_facility(world)
    world.facilities[facility].working_units = 0
    if "infrastructure_v1" in world.model_state:
        world.model_state["infrastructure_v1"]["base_working_units"][facility] = 0
    capacity = world.facilities[facility].capacity
    schedule_effect_program(
        world,
        _program(
            _action(
                "facility_repair",
                "facility",
                facility,
                {
                    "facility_id": facility,
                    "component_id": facility,
                    "expected_working_units": 0,
                    "restored_working_units": capacity,
                },
                end=None,
            )
        ),
    )
    event = _structure_event(world, "facility_repair")
    handler = EffectProgramStructureHandler()
    state = _same_in_both(rules.structure, handler, world, event)
    assert state.facilities[facility].working_units == capacity
    world.facilities[facility].working_units = capacity
    _refused(rules.structure, handler, world, event)


def test_a_structure_event_with_other_fields_goes_to_python(world: WorldState, rules: Any) -> None:
    schedule_effect_program(
        world, _program(_action("add_staff", "zone", _a_zone(world), {"count": 2}, end=None))
    )
    document = _structure_event(world, "add_staff").to_dict()
    document["payload"]["other"] = 1
    _refused(
        rules.structure,
        EffectProgramStructureHandler(),
        world,
        ScheduledEvent(**document),
    )


# ---- The guard --------------------------------------------------------------------------------


def _guarded(world: WorldState, guard: dict[str, Any], payload: dict[str, Any]) -> ScheduledEvent:
    return ScheduledEvent(
        event_id="guarded-event",
        kind="intervention",
        due_minute=1,
        priority=0,
        sequence=999_999,
        event_type="effect_program_apply",
        payload=payload,
        source_id=None,
        causation_id=None,
        provenance={KEY: {"guard": guard, "program_id": "no-such-program"}},
    )


def _guard_cases(world: WorldState) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    facility = _a_facility(world)
    zone = world.facilities[facility].zone_id
    staff_id = sorted(world.staff)[0]
    capacity = world.facilities[facility].capacity
    values: list[Any] = [
        0,
        1,
        capacity,
        capacity + 1,
        0.0,
        1.0,
        float(capacity),
        capacity + 0.5,
        -1.5,
        True,
        False,
        "1",
        "",
    ]
    payloads = (
        {"facility_id": facility},
        {"zone_id": zone},
        {"staff_id": staff_id},
        {"resource_id": "water_ml"},
        {"resource_id": "other"},
        {"target_id": facility},
        {"facility_id": "no-such-facility"},
        {},
    )
    cases: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for field in (
        "simulation.minute",
        "target.capacity",
        "target.working_units",
        "target.queue_length",
        "zone.occupancy",
        "resource.available_quantity",
        "staff.available",
        "no.such.field",
    ):
        for operator in ("eq", "ne", "gt", "gte", "lt", "lte", "other"):
            for value in values:
                for payload in payloads:
                    cases.append(({"operator": operator, "field": field, "value": value}, payload))
    return cases


def test_each_guard_is_python_s_answer_or_python_s_to_give(world: WorldState, rules: Any) -> None:
    generator = random.Random(7)
    cases = _guard_cases(world)
    generator.shuffle(cases)
    answered = refused = 0
    for guard, payload in cases[:6000]:
        event = _guarded(world, guard, payload)
        try:
            expected: Any = should_apply_effect_guard.__wrapped__(world, event)
        except Exception as raised:  # noqa: BLE001 - only that it raised
            expected = raised
        try:
            answer = rules.should_apply_guard(world, event)
        except native.Unsupported:
            refused += 1
            continue
        answered += 1
        assert answer is expected, (guard, payload, answer, expected)
    # The test means nothing if the kernel only ever refuses.
    assert answered > 500 and refused > 500


def _random_guard(generator: random.Random, depth: int = 1) -> dict[str, Any]:
    if depth < 6 and generator.random() < 0.5:
        count = generator.choice((1, 2, 2, 3, 4, 5))
        return {
            "operator": generator.choice(("and", "or")),
            "operands": [_random_guard(generator, depth + 1) for _ in range(count)],
        }
    guard: dict[str, Any] = {
        "operator": generator.choice(("eq", "ne", "gt", "gte", "lt", "lte")),
        "field": generator.choice(("simulation.minute", "target.capacity", "staff.available")),
        "value": generator.choice((0, 1, 1.0, 0.5, True, False, "x", "y" * 257, None)),
    }
    if generator.random() < 0.05:
        guard["other"] = 1
    if generator.random() < 0.05:
        guard["operands"] = [{"operator": "eq"}]
    return guard


def test_a_guard_the_contract_refuses_is_refused(world: WorldState, rules: Any) -> None:
    generator = random.Random(11)
    facility = _a_facility(world)
    payload = {"facility_id": facility, "staff_id": sorted(world.staff)[0]}
    accepted = 0
    for _ in range(1500):
        guard = _random_guard(generator)
        event = _guarded(world, guard, payload)
        try:
            answer = rules.should_apply_guard(world, event)
        except native.Unsupported:
            continue
        accepted += 1
        # What the kernel takes, the contract takes, with the same answer.
        GuardExpression.model_validate(event.provenance[KEY]["guard"])
        assert answer is should_apply_effect_guard.__wrapped__(world, event)
    assert accepted > 50


def _nested(depth: int) -> dict[str, Any]:
    leaf = {"operator": "gte", "field": "simulation.minute", "value": 0}
    node = leaf
    for _ in range(depth - 1):
        node = {"operator": "and", "operands": [node, leaf]}
    return node


def _wide(operands: int) -> dict[str, Any]:
    leaf = {"operator": "gte", "field": "simulation.minute", "value": 0}
    return {"operator": "and", "operands": [leaf] * operands}


@pytest.mark.parametrize(
    ("taken", "turned_down"),
    [
        (_nested(4), _nested(5)),
        (_wide(4), _wide(5)),
        # Each of four operands with four leaves is 21 nodes; with three of them, 17.
        (
            {"operator": "or", "operands": [_wide(4), _wide(4), _wide(4)]},
            {"operator": "or", "operands": [_wide(4), _wide(4), _wide(4), _wide(4)]},
        ),
        (
            {"operator": "eq", "field": "simulation.minute", "value": "x" * 256},
            {"operator": "eq", "field": "simulation.minute", "value": "x" * 257},
        ),
    ],
)
def test_the_bounds_of_a_guard_are_the_contract_s(
    world: WorldState, rules: Any, taken: dict[str, Any], turned_down: dict[str, Any]
) -> None:
    event = _guarded(world, taken, {})
    assert rules.should_apply_guard(world, event) is should_apply_effect_guard.__wrapped__(
        world, event
    )
    event = _guarded(world, turned_down, {})
    with pytest.raises(native.Unsupported):
        rules.should_apply_guard(world, event)
    with pytest.raises(EventApplicationError):
        should_apply_effect_guard.__wrapped__(world, event)


def test_the_size_of_a_guard_counts_to_twenty(world: WorldState, rules: Any) -> None:
    def tree(nodes: int) -> dict[str, Any]:
        # Under the root, three-leaf ands of 4 nodes and, for what is left, leaves.
        leaf = {"operator": "gte", "field": "simulation.minute", "value": 0}
        three = {"operator": "and", "operands": [leaf, leaf, leaf]}
        wider = {"operator": "and", "operands": [leaf, leaf, leaf, leaf]}
        children = {20: [wider, wider, wider, three], 21: [wider, wider, wider, wider]}
        return {"operator": "and", "operands": children[nodes]}

    event = _guarded(world, tree(20), {})
    assert rules.should_apply_guard(world, event) is True
    assert should_apply_effect_guard.__wrapped__(world, event) is True
    event = _guarded(world, tree(21), {})
    with pytest.raises(native.Unsupported):
        rules.should_apply_guard(world, event)
    with pytest.raises(EventApplicationError):
        should_apply_effect_guard.__wrapped__(world, event)


def test_large_int_against_a_float_is_python_s(world: WorldState, rules: Any) -> None:
    """Python compares exactly; a cast to f64 would round 2**53 + 1."""
    world.minute_index = 2**53 + 1
    event = _guarded(
        world, {"operator": "eq", "field": "simulation.minute", "value": float(2**53)}, {}
    )
    with pytest.raises(native.Unsupported):
        rules.should_apply_guard(world, event)
    assert should_apply_effect_guard.__wrapped__(world, event) is False


def test_an_event_without_a_marker_applies(world: WorldState, rules: Any) -> None:
    event = ScheduledEvent(
        event_id="plain",
        kind="intervention",
        due_minute=1,
        priority=0,
        sequence=999_999,
        event_type="effect_program_apply",
        payload={},
        source_id=None,
        causation_id=None,
        provenance={},
    )
    assert rules.should_apply_guard(world, event) is True


# ---- What other modules ask, and the state ----------------------------------------------------


def test_a_pending_modifier_is_seen(world: WorldState, rules: Any) -> None:
    facility = _a_facility(world)
    assert not rules.has_pending_modifier(world, facility)
    schedule_effect_program(
        world,
        _program(_action("set_facility_availability", "facility", facility, {"working_units": 0})),
    )
    assert rules.has_pending_modifier(world, facility) is True
    assert has_pending_facility_modifier.__wrapped__(world, facility) is True


def test_the_operators_and_event_types_are_the_registry_s(rules: Any) -> None:
    assert sorted(rules.OPERATORS) == sorted(
        spec.op for spec in DEFAULT_PRIMITIVE_REGISTRY.operators
    )
    for phase in ("start", "expiry", "derived"):
        for op in rules.OPERATORS:
            assert rules.event_type_of(phase, op) == EFFECT_EVENT_TYPES[phase].get(op), (phase, op)


def test_the_row_writes_the_bytes_of_the_tree(world: WorldState) -> None:
    facility = _a_facility(world)
    schedule_effect_program(
        world,
        _program(_action("set_facility_availability", "facility", facility, {"working_units": 0})),
    )
    mirror = world.model_state._mirrors[KEY]
    assert mirror.verify()
    assert mirror.tainted is None
    assert mirror.row_json() is not None


def test_scheduling_writes_no_whole_namespace_once_it_exists(world: WorldState) -> None:
    facility = _a_facility(world)
    schedule_effect_program(
        world,
        _program(_action("set_facility_availability", "facility", facility, {"working_units": 0})),
    )
    mirror = world.model_state._mirrors[KEY]
    before = mirror.stats()["whole_writes"]
    EffectProgramFacilityHandler().apply(world, _event(world, "effect_program_apply"))
    # The handler's copy is a new world with a new mirror; what it wrote is a record at a time.
    assert world.model_state._mirrors[KEY].stats()["whole_writes"] <= before


def test_a_row_taken_by_a_bad_value_goes_to_python(world: WorldState, rules: Any) -> None:
    facility = _a_facility(world)
    schedule_effect_program(
        world,
        _program(_action("set_facility_availability", "facility", facility, {"working_units": 0})),
    )
    world.model_state[KEY]["schema_version"] = 1.0
    assert world.model_state._mirrors[KEY].tainted is not None
    with pytest.raises(native.Unsupported):
        rules.validate_state(world, validate_event_history=False)
    # 1.0 == 1 passes the Python rule, so only the row is lost.
    validate_effect_program_state.__wrapped__(world, validate_event_history=False)
    with pytest.raises(native.Unsupported):
        rules.has_active_modifier(world, facility)


def _random_write(generator: random.Random, world: WorldState) -> None:
    """One value somewhere in the namespace or its events, written as code would write it."""
    state = world.model_state[KEY]
    bad: list[Any] = [None, 0, 1, -1, 1.5, "", "x" * 2000, "bad id!", True, [], {}, [1], {"a": 1}]
    value = generator.choice(bad)
    program = next(iter(state["programs"].values()))
    target = generator.choice(
        [
            (state, "schema_version"),
            (state, "other"),
            (program, "program_hash"),
            (program, "source_text_hash"),
            (program, "narrative"),
            (program, "support_mode"),
            (program, "compiler_provenance"),
            (program, "assumptions"),
            (program, "effect_ids"),
            (program, "operators"),
            (program, "scheduled_event_ids"),
            (program, "other"),
            (program["compiler_provenance"], "compiler_kind"),
            (program["compiler_provenance"], "compiler_version"),
            (program["compiler_provenance"], "model_id"),
            (program["compiler_provenance"], "prompt_hash"),
            (program["compiler_provenance"], "other"),
            (program["operators"], generator.choice(tuple(program["operators"]))),
            (program["operators"], "other"),
        ]
        + [
            (record, key)
            for record in state["active_facility_modifiers"].values()
            for key in ("baseline_working_units", "modifiers", "other")
        ]
        + [
            (item, key)
            for record in state["active_facility_modifiers"].values()
            for item in record["modifiers"]
            for key in ("modifier_id", "working_units", "start_minute", "sequence", "other")
        ]
    )
    target[0][target[1]] = value


def test_a_state_the_check_accepts_passes_the_python_rules(world: WorldState, rules: Any) -> None:
    facility = _a_facility(world)
    schedule_effect_program(
        world,
        _program(_action("set_facility_availability", "facility", facility, {"working_units": 0})),
    )
    EffectProgramFacilityHandler().apply(world, _event(world, "effect_program_apply"))
    generator = random.Random(3)
    accepted = refused = 0
    for _ in range(300):
        broken = _copy(world)
        _random_write(generator, broken)
        for history in (True, False):
            try:
                rules.validate_state(broken, validate_event_history=history)
            except native.Unsupported:
                refused += 1
            else:
                accepted += 1
                validate_effect_program_state.__wrapped__(broken, validate_event_history=history)
    assert accepted and refused


def test_the_state_of_a_good_world_is_accepted_with_its_history(
    world: WorldState, rules: Any
) -> None:
    rules.validate_state(world, validate_event_history=True)
    facility = _a_facility(world)
    schedule_effect_program(
        world,
        _program(_action("set_facility_availability", "facility", facility, {"working_units": 0})),
    )
    rules.validate_state(world, validate_event_history=True)
    rules.validate_state(world, validate_event_history=False)
    assert not rust_validated(world, KEY)


@pytest.mark.parametrize(
    ("write", "good"),
    [
        (lambda program: program.__setitem__("narrative", "x" * 1000), True),
        (lambda program: program.__setitem__("narrative", "x" * 1001), False),
        (
            lambda program: program["compiler_provenance"].__setitem__("prompt_hash", "x" * 128),
            True,
        ),
        (
            lambda program: program["compiler_provenance"].__setitem__("prompt_hash", "x" * 129),
            False,
        ),
        (lambda program: program["assumptions"].append(_assumption("x" * 500)), True),
        (lambda program: program["assumptions"].append(_assumption("x" * 501)), False),
    ],
)
def test_the_bounds_of_a_program_record_are_the_python_rule_s(
    world: WorldState, rules: Any, write: Any, good: bool
) -> None:
    facility = _a_facility(world)
    schedule_effect_program(
        world,
        _program(_action("set_facility_availability", "facility", facility, {"working_units": 0})),
    )
    write(world.model_state[KEY]["programs"]["program-1"])
    if good:
        rules.validate_state(world, validate_event_history=True)
        validate_effect_program_state.__wrapped__(world, validate_event_history=True)
    else:
        with pytest.raises(native.Unsupported):
            rules.validate_state(world, validate_event_history=True)
        with pytest.raises(ValueError):
            validate_effect_program_state.__wrapped__(world, validate_event_history=True)


def _assumption(description: str) -> dict[str, Any]:
    return {"assumption_id": "a-1", "description": description, "source_id": "TEST-SOURCE"}


@pytest.mark.parametrize(
    ("place", "value"),
    [
        ("op", "add_staff"),
        ("phase", "other"),
        ("phase", "derived"),
        ("units", "bad id!"),
        ("assumption_ids", ["1", 2]),
        ("source_text_hash", "0" * 64),
        ("guard", {"operator": "eq"}),
        ("other", 1),
    ],
)
def test_a_marker_the_python_rule_refuses_is_refused(
    world: WorldState, rules: Any, place: str, value: Any
) -> None:
    facility = _a_facility(world)
    schedule_effect_program(
        world,
        _program(_action("set_facility_availability", "facility", facility, {"working_units": 0})),
    )
    event = _event(world, "effect_program_apply")
    document = event.to_dict()
    document["provenance"][KEY][place] = value
    world.event_queue.replace(event.event_id, ScheduledEvent(**document))
    with pytest.raises(native.Unsupported):
        rules.validate_state(world, validate_event_history=True)
    with pytest.raises(ValueError):
        validate_effect_program_state.__wrapped__(world, validate_event_history=True)
