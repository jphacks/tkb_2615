"""The trace documents in Rust (rules/trace.rs), against the Python rules' documents, bit by bit.

A Run with a trace goes through the driver and its kernels. Here each model writes its files on the
driver and with the kernels removed, and random worlds reach values no scenario does. Decision D1 of
docs/rust-port/step4-plan.md: the documents keep their shape. See docs/rust-port/lanes/l6-trace.md.
"""

from __future__ import annotations

import json
import random
import sys
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support.paths import BENCHMARKS
from support.python_rules import without_kernels

from sheltersim import native
from sheltersim.core.engine import TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.domain.models import SupportRequestState, SupportRequestStatus
from sheltersim.experiments.phases.update import UpdatePhase
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.policies.alternatives import BoundedStressPolicy
from sheltersim.policies.psychology import (
    IntegratedDistressLifeStatePolicy,
    LifeStatePsychologyPolicy,
    MeanRevertingDistressPolicy,
    PsychologyParameters,
    SimplePsychologyPolicy,
)
from sheltersim.policies.registry import BodyDynamicsFormula

sys.path.insert(0, str(BENCHMARKS))

from gate_scenarios import _model_scenario  # noqa: E402

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

TRACE_FILES = (
    "psychology_contributions.jsonl",
    "psychology_contributions_hourly.jsonl",
    "social_contacts.jsonl",
    "information_contacts.jsonl",
    "decisions.jsonl",
)
MODELS = (
    ("psychology", "causal-stress-v1"),
    ("psychology", "bounded-stress-v1"),
    ("psychology", "mean-reverting-distress-v1"),
    ("psychology", "life-state-feedback-v1"),
    ("psychology", "mean-reverting-life-state-v1"),
    ("social", "sparse-family-zone-contact-v1"),
    ("social", "expressiveness-channel-openness-v1"),
    ("social", "degroot-contact-v1"),
    ("information", "sparse-information-contact-v1"),
    ("information", "hazard-information-v1"),
    ("information", "evidence-aware-rumor-v1"),
)


def _ticks() -> int:
    return native.rules("tick").stats()[0]["ticks"]


def _files(directory: Path) -> dict[str, bytes | None]:
    return {
        name: (directory / name).read_bytes() if (directory / name).exists() else None
        for name in TRACE_FILES
    }


@pytest.mark.parametrize(
    ("domain", "model", "level"),
    [
        *((domain, model, TraceLevel.FULL) for domain, model in MODELS),
        # A summary keeps a sample, the same whichever model made the list.
        ("psychology", "causal-stress-v1", TraceLevel.SUMMARY),
        ("psychology", "mean-reverting-life-state-v1", TraceLevel.SUMMARY),
        ("social", "degroot-contact-v1", TraceLevel.SUMMARY),
        ("information", "hazard-information-v1", TraceLevel.SUMMARY),
    ],
    ids=str,
)
def test_a_run_with_a_trace_writes_the_bytes_of_the_python_rules(
    domain: str, model: str, level: TraceLevel, tmp_path: Path
) -> None:
    scenario = _model_scenario(domain, model)
    before = _ticks()
    run_scenario(scenario, tmp_path / "driver", trace_level=level)
    assert _ticks() - before == scenario.duration_minutes
    driven = _files(tmp_path / "driver")
    with pytest.MonkeyPatch.context() as patch:
        without_kernels(patch)
        run_scenario(scenario, tmp_path / "python", trace_level=level)
    assert driven["psychology_contributions.jsonl"]
    for name, raw in _files(tmp_path / "python").items():
        assert driven[name] == raw, name


def _text(value: Any) -> str:
    return json.dumps([getattr(value, item.name) for item in fields(value)], ensure_ascii=False)


def _shuffle(world: WorldState, draws: random.Random) -> None:
    extremes = (0.0, 1.0)
    zones = sorted(world.zones)
    people = list(world.agents.values())
    for number, agent in enumerate(people):
        agent.zone_id = draws.choice(zones)
        for name in ("toilet_urgency", "hunger", "fatigue", "hydration_need"):
            setattr(agent.needs, name, draws.random() if number % 3 else draws.choice(extremes))
        agent.stress = draws.random() if number % 3 else draws.choice(extremes)
    for number, agent in enumerate(people[:6]):
        status = list(SupportRequestStatus)[number % 4]
        request = SupportRequestState(f"R{number}", agent.id, 0, 10, status=status)
        world.support_requests[request.id] = request
        agent.support_request_id = request.id


def _policies(draws: random.Random, ids: list[str]) -> list[Any]:
    def rate() -> float:
        return draws.choice((0.0, draws.random() * 0.25))

    parameters = PsychologyParameters(
        toilet_growth=rate(),
        hunger_growth=rate(),
        awake_fatigue_growth=rate(),
        resting_fatigue_recovery=rate(),
        sleeping_fatigue_recovery=rate(),
        stress_pressure=rate(),
        queued_stress=rate(),
        resting_stress_recovery=rate(),
        sleeping_stress_recovery=rate(),
        need_pressure_threshold=draws.random(),
        support_wait_stress=rate(),
        natural_recovery_rate_per_minute=rate(),
        baseline_distress_by_agent={ids[0]: draws.random()},
    )
    body = {"uncertainty_drift_per_minute": draws.random() * 0.01}
    formula = BodyDynamicsFormula("body-feedback-complete-v1", "S")
    return [
        SimplePsychologyPolicy(parameters),
        SimplePsychologyPolicy(parameters, include_direct_rest_stress_recovery=False),
        BoundedStressPolicy(parameters),
        MeanRevertingDistressPolicy(parameters),
        LifeStatePsychologyPolicy(parameters, body_config=body),
        LifeStatePsychologyPolicy(parameters, body_config=body, body_formula=formula),
        IntegratedDistressLifeStatePolicy(parameters, body_config=body, body_formula=formula),
    ]


class _Keeping:
    """What a sink is given, as text."""

    def __init__(self, level: TraceLevel) -> None:
        self.trace_level = level
        self.documents: list[str] = []

    def record_psychology_trace(self, trace: Any, *, agent_contributions: Any = ()) -> None:
        self.documents.append(json.dumps([trace, list(agent_contributions)], ensure_ascii=False))


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize(
    "level", [TraceLevel.SUMMARY, TraceLevel.FULL], ids=lambda level: level.value
)
def test_the_rows_and_the_document_of_each_model_are_python_s(
    generated_scenario: Any, tmp_path: Path, seed: int, level: TraceLevel
) -> None:
    rules = native.rules("psychology")
    record = native.rules("trace").record_psychology_trace
    draws = random.Random(seed)
    world = WorldState.from_scenario(generated_scenario)
    artifacts = RunArtifactWriter(generated_scenario, tmp_path, trace_level=TraceLevel.OFF)
    artifacts.write_initial(world, execution_mode="offline")
    controller = OfflineSimulationController(world, artifacts)
    _shuffle(world, draws)
    view = controller._view_after_movement(world)
    for policy in _policies(draws, sorted(world.agents)):
        controller.psychology = policy
        controller.artifacts = _Keeping(level)
        result = rules.update_psychology(controller, view, 0)
        python = policy.explain_update(view, 0)
        assert [_text(trace) for trace in result.traces] == [
            _text(trace) for trace in python.traces
        ]
        assert result.policy_version == python.policy_version
        # What the social step does between the two.
        final = tuple(
            delta.with_stress(draws.random()) if number % 2 else delta
            for number, delta in enumerate(result.deltas)
        )
        record(controller, result, final, 0)
        UpdatePhase._record_psychology_trace(controller, python, final, 0)
        ours, theirs = controller.artifacts.documents
        assert ours == theirs


def test_a_python_result_stays_with_the_python_step(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = SimpleNamespace(
        psychology=SimplePsychologyPolicy(), artifacts=_Keeping(TraceLevel.SUMMARY)
    )
    result = SimpleNamespace(traces=(), deltas=())
    with pytest.raises(native.Unsupported, match="Python rule's"):
        native.rules("trace").record_psychology_trace(controller, result, (), world.minute_index)
    assert controller.artifacts.documents == []


def test_no_trace_writes_nothing(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = SimpleNamespace(
        psychology=SimplePsychologyPolicy(), artifacts=_Keeping(TraceLevel.OFF)
    )
    native.rules("trace").record_psychology_trace(controller, object(), (), world.minute_index)
    assert controller.artifacts.documents == []


def test_a_person_with_no_final_delta_is_left_to_the_python_step(
    generated_scenario: Any, tmp_path: Path
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    artifacts = RunArtifactWriter(generated_scenario, tmp_path, trace_level=TraceLevel.OFF)
    artifacts.write_initial(world, execution_mode="offline")
    controller = OfflineSimulationController(world, artifacts)
    view = controller._view_after_movement(world)
    controller.psychology = SimplePsychologyPolicy()
    controller.artifacts = _Keeping(TraceLevel.FULL)
    result = native.rules("psychology").update_psychology(controller, view, 0)
    with pytest.raises(native.Unsupported, match="no final delta"):
        native.rules("trace").record_psychology_trace(controller, result, result.deltas[:-1], 0)
    assert controller.artifacts.documents == []
