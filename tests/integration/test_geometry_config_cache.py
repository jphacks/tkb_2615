"""Tick phases reuse parsed facility geometry without changing the simulation.

How the per-phase cache itself behaves is checked in
tests/unit/domain/test_geometry_overlay.py.
"""

from __future__ import annotations

from sheltersim.contracts.extensions import FacilityGeometry
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink


def test_tick_phases_reuse_parsed_geometry_without_changing_results(
    generated_scenario, count_validations, monkeypatch
):
    def run(scoped):
        world = WorldState.from_scenario(generated_scenario)
        initialize_simulator_extensions(world)
        controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
        handler = controller if scoped else controller._run_phase
        engine = TickEngine(world, phase_handler=handler)
        calls = count_validations(FacilityGeometry)
        for _ in range(10):
            engine.step()
        monkeypatch.undo()
        return world.canonical_json(), len(calls)

    scoped_world, scoped_parses = run(True)
    unscoped_world, unscoped_parses = run(False)
    assert scoped_world == unscoped_world
    assert scoped_parses * 3 < unscoped_parses
