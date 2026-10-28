"""Invalid EffectPrograms, as an untrusted compiler might produce them, are refused with a
reason before anything is scheduled, for every operation the Project screen can apply:
never with another exception.

Each kind of mistake is applied to one valid effect of each operation on a generated
Scenario's main line. Rules for single parameters are checked in
tests/unit/events/test_effects.py.
"""

from __future__ import annotations

import copy
import itertools

import pytest
from pydantic import ValidationError

from sheltersim.events.effects import (
    DEFAULT_PRIMITIVE_REGISTRY,
    EffectProgram,
    SupportMode,
    TargetSelector,
    TimedAction,
)
from sheltersim.experiments.branch import BranchError
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.projects import digest

END = 60
START = 10
_BRANCHES = itertools.count()

# One valid effect per operation on the generated Scenario: (kind, target, parameters, end).
VALID = {
    "add_toilet": ("zone", "toilet", {"count": 1}, None),
    "add_staff": ("zone", "staff", {"count": 1}, None),
    "add_evacuees": ("zone", "sleeping", {"count": 1, "household_size": 1}, None),
    "set_facility_availability": ("facility", "medical_desk", {"working_units": 0}, 40),
    "facility_outage": ("facility", "medical_desk", {"units": 1}, 40),
    "set_ambient_noise": ("zone", "sleeping", {"level": 0.5}, 40),
    "set_temperature": ("zone", "sleeping", {"temperature_c": 30.0}, 40),
    "set_lighting": ("zone", "sleeping", {"level": 0.5}, 40),
    "set_privacy": ("zone", "sleeping", {"level": 0.5}, 40),
    "set_ventilation": ("zone", "sleeping", {"effectiveness": 0.5}, 40),
    "modify_demand_rate": (
        "zone",
        "sleeping",
        {"action": "rest", "probability_multiplier": 1.5, "duration_multiplier": 1.0},
        40,
    ),
    "add_temporary_space": ("zone", "sleeping", {"people_capacity": 5, "sleeping_capacity": 5}, 40),
    "set_staff_availability": ("staff", "staff_002", {"available": False}, 40),
    "set_staff_role": ("staff", "staff_003", {"skills": ["water_service"]}, None),
    "water_outage": ("facility", "water_station_main", {"outage": True}, 40),
    "set_route_access": ("route", "route_corridor_quiet", {"accessible": False}, 40),
    "move_facility": ("facility", "medical_desk", {"point_m": {"x": 10.4, "y": 5.0}}, None),
}

MISTAKES = {
    "unknown_target": lambda kind, target, params, end, start: (kind, "nope", params, end, start),
    "wrong_target_kind": lambda kind, target, params, end, start: (
        "person" if kind != "person" else "zone",
        target,
        params,
        end,
        start,
    ),
    "start_before_branch": lambda kind, target, params, end, start: (
        kind,
        target,
        params,
        end,
        start - 5,
    ),
    "start_at_scenario_end": lambda kind, target, params, end, start: (
        kind,
        target,
        params,
        end,
        END,
    ),
    "end_before_start": lambda kind, target, params, end, start: (
        kind,
        target,
        params,
        start if end else start - 5,
        start,
    ),
    "end_after_scenario": lambda kind, target, params, end, start: (
        kind,
        target,
        params,
        END + 100,
        start,
    ),
    "unknown_parameter": lambda kind, target, params, end, start: (
        kind,
        target,
        {**params, "unexpected": 1},
        end,
        start,
    ),
}


@pytest.fixture(scope="module")
def main_line(tmp_path_factory, generated_previews, saved_timeline):
    scenario = ScenarioSpec.model_validate(
        copy.deepcopy(generated_previews(duration_days=END / 1440)["scenario"])
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
        _, timeline = saved_timeline(tmp_path_factory.mktemp("validation") / "projects", scenario)
        timeline.advance("main", START, checkpoint_interval=START)
        yield timeline


def _program(op: str, kind: str, target: str, params: dict, end, start: int) -> EffectProgram:
    spec = DEFAULT_PRIMITIVE_REGISTRY.get(op)
    return EffectProgram(
        program_id=f"program-{op}",
        narrative=op,
        source_text=op,
        simulation_start_minute=start,
        effects=[
            TimedAction(
                effect_id="effect",
                op=op,
                target_selector=TargetSelector(kind=kind, target_id=target),
                start_minute=start,
                end_minute=end,
                parameters=params,
                units=sorted(spec.units)[0],
                source_id="TEST-VALIDATION",
                assumption_ids=[],
                policy_dependencies=sorted(spec.required_policies),
                reversible=end is not None,
            )
        ],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


def _fork(timeline, program: EffectProgram) -> None:
    timeline.fork(
        "main",
        START,
        f"checked-{next(_BRANCHES)}",
        program=program,
        approved_program_hash=digest(program.model_dump(mode="json")),
    )


def test_every_valid_effect_is_accepted(main_line):
    for op, (kind, target, params, end) in VALID.items():
        _fork(main_line, _program(op, kind, target, params, end, START))


@pytest.mark.parametrize("mistake", sorted(MISTAKES))
def test_a_mistake_is_refused_with_a_reason_for_every_operation(main_line, mistake):
    for op, valid in VALID.items():
        kind, target, params, end, start = MISTAKES[mistake](*valid, START)
        try:
            program = _program(op, kind, target, params, end, start)
        except ValidationError:
            continue  # Refused by the contract itself.
        with pytest.raises(BranchError):
            _fork(main_line, program)
