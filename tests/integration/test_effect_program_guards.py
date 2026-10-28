"""EffectProgram guards on a generated Scenario: a false guard suppresses an effect's start
and its expiry alike, an and-guard applies when every operand holds, and a guard on a
floor plan change reads its facility or zone target (#194).

The xfail tests are defects found while covering these paths (#178): an environment
expiry after a suppressed start is recorded as failed (#195), and eq compares an
integer with a float by type (#196).
"""

from __future__ import annotations

import copy
import itertools

import pytest

from sheltersim.events.effects import (
    DEFAULT_PRIMITIVE_REGISTRY,
    EffectProgram,
    GuardExpression,
    SupportMode,
    TargetSelector,
    TimedAction,
)
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.projects import digest

END = 60
START, STOP = 10, 40
_BRANCHES = itertools.count()


@pytest.fixture(scope="module")
def main_line(tmp_path_factory, generated_previews, saved_timeline):
    """The generated Scenario's main line at the start minute; tests fork their own Branches."""
    scenario = ScenarioSpec.model_validate(
        copy.deepcopy(generated_previews(duration_days=END / 1440)["scenario"])
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
        _, timeline = saved_timeline(tmp_path_factory.mktemp("guards") / "projects", scenario)
        timeline.advance("main", START, checkpoint_interval=START)
        yield timeline


def _leaf(field: str, operator: str, value: object) -> GuardExpression:
    return GuardExpression(operator=operator, field=field, value=value)


def _outcomes(timeline, op, kind, target, parameters, guard, *, end=STOP):
    """Fork with one guarded effect, run to END and return its (minute, status, detail)."""
    spec = DEFAULT_PRIMITIVE_REGISTRY.get(op)
    program = EffectProgram(
        program_id=f"program-{op}",
        narrative=op,
        source_text=op,
        simulation_start_minute=START,
        effects=[
            TimedAction(
                effect_id="guarded",
                op=op,
                target_selector=TargetSelector(kind=kind, target_id=target),
                start_minute=START,
                end_minute=end,
                parameters=parameters,
                units=sorted(spec.units)[0],
                source_id="TEST-GUARD",
                assumption_ids=[],
                policy_dependencies=sorted(spec.required_policies),
                reversible=end is not None,
                guard=guard,
            )
        ],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )
    branch_id = f"guarded-{next(_BRANCHES)}"
    timeline.fork(
        "main",
        START,
        branch_id,
        program=program,
        approved_program_hash=digest(program.model_dump(mode="json")),
    )
    world = timeline.advance(branch_id, END, checkpoint_interval=END).world
    return [
        (record.realized_minute, record.status.value, record.detail)
        for record in world.event_log.records
        if "effect_program_v1" in record.event.provenance
    ]


NEVER = _leaf("simulation.minute", "gte", 100_000)


@pytest.mark.parametrize(
    "op,kind,target,parameters",
    [
        ("set_facility_availability", "facility", "medical_desk", {"working_units": 0}),
        ("add_temporary_space", "zone", "sleeping", {"people_capacity": 5, "sleeping_capacity": 5}),
        ("set_route_access", "route", "route_corridor_quiet", {"accessible": False}),
    ],
)
def test_a_false_guard_suppresses_the_start_and_its_expiry(main_line, op, kind, target, parameters):
    outcomes = _outcomes(main_line, op, kind, target, parameters, NEVER)

    assert [(minute, status) for minute, status, _ in outcomes] == [
        (START, "suppressed"),
        (STOP, "suppressed"),
    ]
    assert outcomes[0][2] == "EffectProgram guard evaluated false"


def test_an_and_guard_applies_when_every_operand_holds(main_line):
    guard = GuardExpression(
        operator="and",
        operands=[_leaf("zone.occupancy", "gte", 0), _leaf("simulation.minute", "lt", END)],
    )

    outcomes = _outcomes(main_line, "set_ambient_noise", "zone", "sleeping", {"level": 0.8}, guard)

    assert [(minute, status) for minute, status, _ in outcomes] == [
        (START, "applied"),
        (STOP, "applied"),
    ]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#195: an environment expiry after a suppressed start is recorded as failed",
)
def test_an_environment_change_with_a_false_guard_has_a_suppressed_expiry(main_line):
    outcomes = _outcomes(main_line, "set_ambient_noise", "zone", "sleeping", {"level": 0.8}, NEVER)

    assert [(minute, status) for minute, status, _ in outcomes] == [
        (START, "suppressed"),
        (STOP, "suppressed"),
    ]


# Floor plan changes keep their target in target_id; guards used to ignore it (#194).
MOVE_DESK = ("move_facility", "facility", "medical_desk", {"point_m": {"x": 10.4, "y": 5.0}})
CORNER_PARTITION = (
    "set_partition",
    "zone",
    "medical",
    {
        "polygon_m": [
            {"x": 9.6, "y": 11.9},
            {"x": 9.9, "y": 11.9},
            {"x": 9.9, "y": 12.3},
            {"x": 9.6, "y": 12.3},
        ]
    },
)


@pytest.mark.parametrize(
    "change,guard,end",
    [
        (MOVE_DESK, _leaf("target.working_units", "eq", 1), None),
        (CORNER_PARTITION, _leaf("zone.occupancy", "gte", 0), STOP),
    ],
)
def test_a_true_guard_on_a_floor_plan_change_applies_it(main_line, change, guard, end):
    outcomes = _outcomes(main_line, *change, guard, end=end)

    assert outcomes[0][:2] == (START, "applied"), outcomes[0]


@pytest.mark.parametrize(
    "change,guard,end",
    [
        (MOVE_DESK, _leaf("target.working_units", "eq", 99), None),
        (CORNER_PARTITION, _leaf("zone.occupancy", "gt", 100_000), STOP),
    ],
)
def test_a_false_guard_on_a_floor_plan_change_suppresses_it(main_line, change, guard, end):
    outcomes = _outcomes(main_line, *change, guard, end=end)

    assert {status for _, status, _ in outcomes} == {"suppressed"}
    assert outcomes[0][:2] == (START, "suppressed")
    assert outcomes[0][2] == "EffectProgram guard evaluated false"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#196: eq compares an integer field with a float value by type",
)
def test_eq_compares_numbers_by_value(main_line):
    if main_line.seek("main", START).world.facilities["medical_desk"].queue_agent_ids:
        pytest.fail("the medical desk queue must be empty at the start")
    guard = _leaf("target.queue_length", "eq", 0.0)

    outcomes = _outcomes(
        main_line,
        "set_facility_availability",
        "facility",
        "medical_desk",
        {"working_units": 0},
        guard,
    )

    assert outcomes[0][:2] == (START, "applied"), outcomes[0]
