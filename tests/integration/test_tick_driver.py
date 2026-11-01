"""The Rust driver of a tick (rules/tick.rs): the phases' results, and what sends a tick to them.

The shadows of benchmarks/rule_shadow go through Python entries, which the driver doesn't, so here
the driver and the phases run the same scenario and all they saved is compared, also after each
phase.
See docs/rust-port/lanes/m6-driver.md.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest
from support.paths import BENCHMARKS

from sheltersim import native
from sheltersim.core.engine import (
    TICK_PHASE_ORDER,
    TickEngine,
    TickExecutionError,
    TickPhase,
    TraceLevel,
)
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.experiments.phases.driver import DRIVEN_RULES, DRIVEN_STEPS
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.llm.provider import LLMMode, LLMRouter, LLMSettings
from sheltersim.llm.runtime import EventTriggeredSocialPolicy

sys.path.insert(0, str(BENCHMARKS))

import tick_probe  # noqa: E402

ROOT = BENCHMARKS.parent
needs_rust = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")


def _stats() -> tuple[dict[str, int], dict[str, int]]:
    counts, reasons = native.rules("tick").stats()
    return dict(counts), dict(reasons)


def _ticks() -> int:
    return _stats()[0]["ticks"]


def _build(
    scenario: Any,
    directory: Path,
    *,
    trace: TraceLevel = TraceLevel.OFF,
    router: LLMRouter | None = None,
    rng: Any = None,
) -> tuple[WorldState, OfflineSimulationController, TickEngine]:
    world = WorldState.from_scenario(scenario)
    artifacts = RunArtifactWriter(scenario, directory, trace_level=trace)
    artifacts.write_initial(world, execution_mode="offline")
    controller = OfflineSimulationController(world, artifacts, llm_router=router)
    return world, controller, TickEngine(world, phase_handler=controller, rng=rng)


def _saved(world: WorldState, controller: OfflineSimulationController) -> dict[str, Any]:
    return {
        "world": world.canonical_json(),
        "records": [raw.decode() for raw in world.event_log.items_bytes(0, len(world.event_log))],
        "pending": [event.to_dict() for event in world.event_queue.pending()],
        "counters": dict(world.id_counters),
        "metrics": controller.artifacts.metrics.to_dict(),
    }


def _on_the_phases() -> Any:
    class Python:
        def __enter__(self) -> None:
            native.rules("tick").python_path_only(True)

        def __exit__(self, *_exception: object) -> None:
            native.rules("tick").python_path_only(False)

    return Python()


def _run(scenario: Any, directory: Path, minutes: int, **options: Any) -> dict[str, Any]:
    world, controller, engine = _build(scenario, directory, **options)
    engine.run(minutes)
    return _saved(world, controller)


@pytest.fixture
def short_scenario(generated_scenario: Any) -> Any:
    """Ends where these tests run to, so the Run's last minute is reached too."""
    return generated_scenario.model_copy(update={"duration_minutes": 240})


def test_only_the_rust_kernels_give_a_controller_a_driver() -> None:
    assert hasattr(OfflineSimulationController, "run_tick") == native.available()


@needs_rust
def test_a_whole_run_is_the_same_through_the_driver_and_through_the_phases(
    short_scenario: Any, tmp_path: Path
) -> None:
    minutes = short_scenario.duration_minutes
    before = _ticks()
    driven = _run(short_scenario, tmp_path / "driven", minutes)
    assert _ticks() - before == minutes
    with _on_the_phases():
        phases = _run(short_scenario, tmp_path / "phases", minutes)
    assert _ticks() - before == minutes, "the phases' run went through the driver"
    assert driven == phases
    # The last minute closes what is open, and the run did something to compare.
    assert len(driven["records"]) > 100


@needs_rust
def test_each_phase_ends_in_the_same_state_through_the_driver_and_the_phases(
    generated_scenario: Any, tmp_path: Path
) -> None:
    def digests(directory: Path) -> list[tuple[str, bool, int, str]]:
        world, _controller, engine = _build(generated_scenario, directory)
        seen: list[tuple[str, bool, int, str]] = []

        def watch(phase: TickPhase, before: bool) -> None:
            seen.append(
                (phase.value, before, world.minute_index, world.state_digest()),
            )

        engine.phase_observer = watch
        engine.run(90)
        return seen

    before = _ticks()
    driven = digests(tmp_path / "driven")
    assert _ticks() - before == 90
    with _on_the_phases():
        phases = digests(tmp_path / "phases")
    assert driven == phases
    assert len(driven) == 90 * 2 * len(TICK_PHASE_ORDER)
    assert len({item[3] for item in driven}) > 90, "the digests did not see the phases' work"


@needs_rust
def test_the_driver_keeps_the_view_the_next_phase_builds_on(
    generated_scenario: Any, tmp_path: Path
) -> None:
    """Only speed shows if it doesn't: the proposals' view is then built from the world."""
    _world, controller, engine = _build(generated_scenario, tmp_path)
    kept: list[bool] = []

    def watch(phase: TickPhase, before: bool) -> None:
        if not before and phase is TickPhase.UPDATE_AGENT_STATE:
            kept.append(controller._view_before_proposals is not None)
        if not before and phase is TickPhase.PROPOSE_ACTIONS:
            kept.append(controller._view_before_proposals is None)

    engine.phase_observer = watch
    before = _ticks()
    engine.run(2)
    assert _ticks() - before == 2
    assert kept == [True] * 4


@needs_rust
def test_a_step_a_kernel_refuses_goes_to_its_python_rule(
    generated_scenario: Any, tmp_path: Path
) -> None:
    class OtherRng(CounterRNG):
        """Not plainly a CounterRNG, so what draws in Rust says Unsupported."""

    seed = generated_scenario.seed
    before = _stats()[0]
    driven = _run(generated_scenario, tmp_path / "driven", 120, rng=OtherRng(seed))
    after = _stats()[0]
    assert after["ticks"] - before["ticks"] == 120
    assert after["kernels_refused"] - before["kernels_refused"] >= 120
    with _on_the_phases():
        phases = _run(generated_scenario, tmp_path / "phases", 120, rng=OtherRng(seed))
    plain = _run(generated_scenario, tmp_path / "plain", 120)
    assert driven == phases == plain


@needs_rust
@pytest.mark.parametrize("step", DRIVEN_STEPS)
def test_a_patched_step_sends_the_tick_to_the_phases(
    step: str, generated_scenario: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, controller, engine = _build(generated_scenario, tmp_path)
    original = getattr(OfflineSimulationController, step)

    def patched(self: Any, *arguments: Any, **options: Any) -> Any:
        return original(self, *arguments, **options)

    monkeypatch.setattr(OfflineSimulationController, step, patched)
    state = world.state_digest()
    assert controller.run_tick(world, 0, engine.rng, None) is False
    assert world.state_digest() == state


@needs_rust
@pytest.mark.parametrize(
    "holder, name", DRIVEN_RULES, ids=[f"{item[0].__name__}.{item[1]}" for item in DRIVEN_RULES]
)
def test_a_patched_rule_sends_the_tick_to_the_phases(
    holder: Any, name: str, generated_scenario: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, controller, engine = _build(generated_scenario, tmp_path)
    original = getattr(holder, name)

    def patched(*arguments: Any, **options: Any) -> Any:
        return original(*arguments, **options)

    monkeypatch.setattr(holder, name, patched)
    assert controller.run_tick(world, 0, engine.rng, None) is False


@needs_rust
def test_a_step_patched_on_the_controller_itself_sends_the_tick_to_the_phases(
    generated_scenario: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, controller, engine = _build(generated_scenario, tmp_path)
    calls = []
    original = controller._apply_due_proposals

    def counted(*arguments: Any) -> None:
        calls.append(arguments)
        original(*arguments)

    monkeypatch.setattr(controller, "_apply_due_proposals", counted)
    before = _ticks()
    engine.run(3)
    assert len(calls) == 3
    assert _ticks() == before


@needs_rust
@pytest.mark.parametrize("level", [TraceLevel.SUMMARY, TraceLevel.FULL])
def test_a_trace_sends_each_tick_to_the_phases(
    level: TraceLevel, generated_scenario: Any, tmp_path: Path
) -> None:
    before = _ticks()
    _run(generated_scenario, tmp_path, 3, trace=level)
    assert _ticks() == before
    assert _stats()[1]["a trace is recorded"] >= 3


@needs_rust
def test_a_trace_of_the_engine_s_phases_sends_each_tick_to_the_phases(
    generated_scenario: Any, tmp_path: Path
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    artifacts = RunArtifactWriter(generated_scenario, tmp_path, trace_level=TraceLevel.OFF)
    artifacts.write_initial(world, execution_mode="offline")
    seen: list[Any] = []
    engine = TickEngine(
        world,
        phase_handler=OfflineSimulationController(world, artifacts),
        trace_level=TraceLevel.FULL,
        trace_sink=seen.append,
    )
    before = _ticks()
    engine.run(2)
    assert _ticks() == before
    assert [item.phase for item in seen] == list(TICK_PHASE_ORDER) * 2


@needs_rust
def test_a_router_that_can_call_a_provider_sends_each_tick_to_the_phases(
    generated_scenario: Any, tmp_path: Path
) -> None:
    settings = LLMSettings(
        mode=LLMMode.REPLAY,
        max_api_calls=1,
        max_input_tokens=1024,
        max_output_tokens=64,
        budget_usd=0.0,
    )
    before = _ticks()
    _run(generated_scenario, tmp_path, 2, router=LLMRouter(settings))
    assert _ticks() == before
    assert _stats()[1]["a Provider can be called"] >= 2


@needs_rust
def test_the_hooks_of_an_offline_router_run_where_they_run_through_the_phases(
    generated_scenario: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    minutes = generated_scenario.duration_minutes
    calls: list[tuple[str, int, tuple[Any, ...]]] = []
    for name in ("consider_support_request", "consider_complaint_response"):
        original = getattr(EventTriggeredSocialPolicy, name)

        def recorded(self: Any, world: Any, *, _name: str = name, _rule: Any = original, **o: Any):
            calls.append((_name, world.minute_index, tuple(sorted(o.items()))))
            return _rule(self, world, **o)

        monkeypatch.setattr(EventTriggeredSocialPolicy, name, recorded)

    def run(directory: Path) -> tuple[dict[str, Any], list[Any]]:
        calls.clear()
        settings = LLMSettings(
            mode=LLMMode.OFFLINE,
            max_api_calls=1,
            max_input_tokens=1024,
            max_output_tokens=64,
            budget_usd=0.0,
        )
        saved = _run(generated_scenario, directory, minutes, router=LLMRouter(settings))
        return saved, list(calls)

    before = _ticks()
    driven, driven_calls = run(tmp_path / "driven")
    assert _ticks() - before == minutes
    with _on_the_phases():
        phases, phases_calls = run(tmp_path / "phases")
    assert driven == phases
    assert driven_calls == phases_calls
    assert any(call[0] == "consider_support_request" for call in driven_calls)


@needs_rust
def test_a_tick_probe_sends_each_tick_to_the_phases_unless_it_watches_the_driver(
    generated_scenario: Any, tmp_path: Path
) -> None:
    class Seen:
        def __init__(self) -> None:
            self.phases: list[tuple[int, str, bool]] = []

        def engine_started(self, engine: int, world: WorldState) -> None:
            pass

        def before_phase(self, engine: int, world: Any, minute: int, phase: TickPhase) -> None:
            self.phases.append((minute, phase.value, True))

        def after_phase(self, engine: int, world: Any, minute: int, phase: TickPhase) -> None:
            self.phases.append((minute, phase.value, False))

    results = {}
    for on_driver, ticks in ((False, 0), (True, 2)):
        seen = Seen()
        undo = tick_probe.install([seen], on_driver=on_driver)
        before = _ticks()
        try:
            results[on_driver] = _run(generated_scenario, tmp_path / str(on_driver), 2)
        finally:
            undo()
        assert _ticks() - before == ticks
        assert seen.phases == [
            (minute, phase.value, stage)
            for minute in range(2)
            for phase in TICK_PHASE_ORDER
            for stage in (True, False)
        ]
    assert results[False] == results[True]


@needs_rust
def test_the_driver_says_what_the_engine_says_of_a_phase_that_moves_the_clock(
    generated_scenario: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, controller, engine = _build(generated_scenario, tmp_path)
    capture = controller.artifacts.capture_metric

    def moves_the_clock(*arguments: Any, **options: Any) -> Any:
        result = capture(*arguments, **options)
        world.minute_index += 1
        return result

    monkeypatch.setattr(controller.artifacts, "capture_metric", moves_the_clock)
    before = _ticks()
    with pytest.raises(TickExecutionError, match="engine-owned clock"):
        engine.step()
    assert _ticks() - before == 1


@needs_rust
def test_an_error_of_a_rule_comes_through_the_driver_as_itself(
    generated_scenario: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, controller, engine = _build(generated_scenario, tmp_path)

    def fails(*_arguments: Any, **_options: Any) -> None:
        raise ValueError("this rule's own words")

    monkeypatch.setattr(controller.artifacts, "capture_metric", fails)
    with pytest.raises(ValueError, match="this rule's own words"):
        engine.step()


@needs_rust
def test_a_world_with_another_controller_is_refused_with_nothing_changed(
    generated_scenario: Any, tmp_path: Path
) -> None:
    world, controller, engine = _build(generated_scenario, tmp_path / "first")
    other = WorldState.from_scenario(generated_scenario)
    state = world.state_digest()
    assert controller.run_tick(other, 0, engine.rng, None) is False
    assert world.state_digest() == state


# What each phase's steps are called in rules/tick.rs, in the order of the phase's method.
_PHASE_STEPS = {
    ("update", "UpdatePhase", "_run_update_phase", "fn update(&self)"): {
        "_update_movement": "movement::advance_movement(",
        "_view_after_movement": "world_view::view_after_movement(",
        "_psychology_update": "psychology::update_psychology(",
        "_social_propagation": "social_propagation::update_social_propagation(",
        "_update_long_term_psychology": "psychology::update_long_term(",
        "_apply_state_deltas": "psychology::apply_deltas(",
    },
    ("propose", "ProposePhase", "_run_propose_phase", "fn propose(&self)"): {
        "_view_for_proposals": "world_view::view_for_proposals(",
        "_apply_due_proposals": "decisions::propose_rows(",
    },
    ("arbitrate", "ArbitratePhase", "_run_arbitrate_phase", "fn arbitrate(&self)"): {
        "update_staff_dynamics": "staff_dynamics::update(",
        "update_staff_operations": "staff::update_operations(",
        "_arbitrate_services": "// `_arbitrate_services`.",
        "_assign_staff_support": "// `_assign_staff_support`.",
        "assign_staff_operations": "staff::assign_operations(",
        "_consider_due_complaints": '"_consider_due_complaints"',
        "schedule_staff_operation_relief": "staff::relief(",
    },
    ("sample", "SamplePhase", "_run_sample_phase", "fn sample(&self)"): {
        "_sample_information_contacts": "information::sample_contacts(",
        "_sample_proximity_contacts": '"_sample_proximity_contacts"',
    },
}


def _called_names(method: ast.FunctionDef) -> list[str]:
    names = []
    for statement in method.body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Call):
                called = node.func
                name = called.attr if isinstance(called, ast.Attribute) else called.id
                names.append(name)
    return names


@pytest.mark.parametrize(
    "module, owner, method, rust_function", _PHASE_STEPS, ids=[key[0] for key in _PHASE_STEPS]
)
def test_the_driver_takes_a_phase_s_steps_in_the_order_of_its_python_method(
    module: str, owner: str, method: str, rust_function: str
) -> None:
    """Most scenarios can't see two steps swapped when neither writes in the same minute."""
    source = (ROOT / "src/sheltersim/experiments/phases" / f"{module}.py").read_text("utf-8")
    phase = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == owner
    )
    body = next(
        node for node in phase.body if isinstance(node, ast.FunctionDef) and node.name == method
    )
    steps = _PHASE_STEPS[(module, owner, method, rust_function)]
    in_python = [name for name in _called_names(body) if name in steps]
    assert in_python == list(steps)
    rust = (ROOT / "native/src/rules/tick.rs").read_text("utf-8")
    start = rust.index(rust_function)
    end = rust.index("\n    }\n", start)
    places = [rust.index(steps[name], start, end) for name in in_python]
    assert places == sorted(places)


def test_the_driver_takes_the_phases_in_the_engine_s_order() -> None:
    rust = (ROOT / "native/src/rules/tick.rs").read_text("utf-8")
    start = rust.index("fn phase(&self, index: usize)")
    bodies = {
        TickPhase.DISPATCH_DUE_EVENTS: "0 => self.dispatch()",
        TickPhase.COMPLETE_SCHEDULED_TASKS: "1 => Ok(())",
        TickPhase.UPDATE_AGENT_STATE: "2 => self.update()",
        TickPhase.PROPOSE_ACTIONS: "3 => self.propose()",
        TickPhase.ARBITRATE_ACTIONS: "4 => self.arbitrate()",
        TickPhase.SAMPLE_ENDOGENOUS_EVENTS: "5 => self.sample()",
        TickPhase.COLLECT_METRICS: "6 => self.metrics()",
    }
    assert all(rust.find(bodies[phase], start) > 0 for phase in TICK_PHASE_ORDER)
    assert list(bodies) == list(TICK_PHASE_ORDER)
