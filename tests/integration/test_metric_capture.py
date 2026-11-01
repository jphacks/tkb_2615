"""rules/metrics.rs's capture leaves what MetricsRecorder.capture leaves, or says Unsupported first.

The driver calls it for a `_MetricSink` (rules/tick.rs). The fields of a snapshot are
tests/integration/test_entity_rules_kernel.py's. Here, what is around them: the object, the order,
the resource rows and the errors, which stay the Python rule's.
"""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.experiments.metrics import MetricsRecorder, MetricSnapshot
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _IndexedMetricSink, _MetricSink

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")


@pytest.fixture(scope="module")
def documents(tmp_path_factory, generated_scenario_preview) -> list[dict]:
    scenario = ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    taken: list[dict] = []
    step = TickEngine.step

    def recording(engine: TickEngine) -> Any:
        result = step(engine)
        if engine.world.minute_index % 30 == 0 and engine.world.minute_index > 0:
            taken.append(engine.world.to_dict())
        return result

    patch = pytest.MonkeyPatch()
    patch.setattr(TickEngine, "step", recording)
    try:
        run_scenario(scenario, tmp_path_factory.mktemp("run"))
    finally:
        patch.undo()
    assert taken
    return taken


def _kernel(world: Any, recorder: MetricsRecorder, minute: Any) -> None:
    native.rules("metrics").capture(world, recorder, minute)


def _world(document: dict) -> WorldState:
    world = WorldState.from_dict(copy.deepcopy(document))
    world.validate_after_tick()
    return world


def test_a_capture_leaves_what_the_python_rule_leaves(documents) -> None:
    by_kernel, by_python = MetricsRecorder(), MetricsRecorder()
    for document in documents:
        world = _world(document)
        minute = world.minute_index
        _kernel(world, by_kernel, minute)
        by_python.capture(world, minute=minute)
    assert by_kernel.snapshots == by_python.snapshots
    assert by_kernel.resource_snapshots == by_python.resource_snapshots
    assert by_kernel.to_dict() == by_python.to_dict()
    for ours, theirs in zip(by_kernel.snapshots, by_python.snapshots, strict=True):
        assert type(ours) is MetricSnapshot
        assert list(ours.__dict__) == list(theirs.__dict__)
        assert repr(asdict(ours)) == repr(asdict(theirs))
    with pytest.raises(AttributeError):
        by_kernel.snapshots[0].minute_index = 1


@pytest.mark.parametrize(
    "what",
    ["another minute", "not an int", "a minute already taken", "ledger removed", "ledger late"],
)
def test_what_the_python_rule_raises_is_left_to_it_with_nothing_changed(documents, what) -> None:
    world = _world(documents[1])
    recorder = MetricsRecorder()
    minute: Any = world.minute_index
    if what == "another minute":
        minute += 1
    elif what == "not an int":
        minute = float(minute)
    elif what == "a minute already taken":
        recorder.capture(world, minute=minute)
    elif what == "ledger removed":
        earlier = _world(documents[0])
        recorder.capture(earlier, minute=earlier.minute_index)
        del world.model_state["resource_ledger_v1"]
    elif what == "ledger late":
        earlier = _world(documents[0])
        del earlier.model_state["resource_ledger_v1"]
        recorder.capture(earlier, minute=earlier.minute_index)
    before = (list(recorder.snapshots), copy.deepcopy(recorder.resource_snapshots))
    with pytest.raises(native.Unsupported):
        _kernel(world, recorder, minute)
    assert (recorder.snapshots, recorder.resource_snapshots) == before
    if what != "not an int":
        with pytest.raises(ValueError):
            recorder.capture(world, minute=minute)


def test_a_sink_with_another_capture_is_called(
    generated_scenario_preview, tmp_path, monkeypatch
) -> None:
    """Through the driver, for a patch of the class that holds the method. An instance's patch is
    tests/integration/test_tick_driver.py's."""
    scenario = ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    assert _IndexedMetricSink.capture_metric is _MetricSink.capture_metric_as_defined
    calls: list[Any] = []
    defined = _MetricSink.capture_metric

    def patched(self: Any, world: Any, *, minute: Any = None) -> None:
        calls.append(minute)
        defined(self, world, minute=minute)

    monkeypatch.setattr(_MetricSink, "capture_metric", patched)
    monkeypatch.setattr(_MetricSink, "capture_metric_as_defined", defined)
    sink = _MetricSink(MetricsRecorder())
    world = _run_one_tick(scenario, sink)
    assert calls == [0]
    assert sink.metrics.snapshots[0].minute_index == world.minute_index - 1


def _run_one_tick(scenario: ScenarioSpec, sink: _MetricSink) -> WorldState:
    world = WorldState.from_scenario(scenario)
    controller = OfflineSimulationController(world, sink)
    engine = TickEngine(world, phase_handler=controller)
    engine.step()
    return world
