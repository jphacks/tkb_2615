"""Facility geometry is parsed once per tick phase without hiding config edits."""

from copy import deepcopy

import pytest

from sheltersim.contracts.extensions import FacilityGeometry
from sheltersim.contracts.spatial import SpatialConstraints
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.domain.geometry_overlay import (
    GEOMETRY_OVERLAY_KEY,
    effective_geometry,
    effective_spatial_constraints,
    geometry_config_scope,
)
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.experiments.initial_scenario import InitialScenarioBuilder, InitialScenarioRequest
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink


@pytest.fixture(scope="module")
def scenario():
    return InitialScenarioBuilder().build(InitialScenarioRequest(
        municipality="Synthetic municipality", disaster="earthquake", evacuees=20,
        duration_days=1, seed=2026)).scenario


def counting(monkeypatch, model):
    calls = []
    original = model.model_validate.__func__

    def validate(cls, value, *args, **kwargs):
        calls.append(id(value))
        return original(cls, value, *args, **kwargs)

    monkeypatch.setattr(model, "model_validate", classmethod(validate))
    return calls


def test_scope_parses_each_config_object_once(scenario, monkeypatch):
    world = WorldState.from_scenario(scenario)
    raw = world.model_configs["facility_geometry"]
    expected = FacilityGeometry.model_validate(deepcopy(raw))
    other = WorldState.from_dict(world.to_dict())
    geometry_calls = counting(monkeypatch, FacilityGeometry)
    spatial_calls = counting(monkeypatch, SpatialConstraints)
    with geometry_config_scope():
        assert all(effective_geometry(world) == expected for _ in range(50))
        for _ in range(50):
            effective_spatial_constraints(world)
        # A restored or forked world owns new config objects and is parsed for itself.
        assert effective_geometry(other) == expected
    assert geometry_calls == [id(raw), id(other.model_configs["facility_geometry"])]
    assert len(spatial_calls) == 1


def test_config_edits_outside_a_scope_are_always_seen(scenario):
    world = WorldState.from_scenario(scenario)
    with geometry_config_scope():
        assert effective_geometry(world).routes
    world.model_configs["facility_geometry"]["routes"] = []
    assert effective_geometry(world).routes == ()
    with geometry_config_scope():
        assert effective_geometry(world).routes == ()


def test_runtime_overrides_still_apply_inside_a_scope(scenario):
    world = WorldState.from_scenario(scenario)
    with geometry_config_scope():
        base = effective_geometry(world)
        facility_id, point = next(iter(base.facility_points.items()))
        moved = {"x": point.x + 1.0, "y": point.y}
        world.model_state.setdefault(GEOMETRY_OVERLAY_KEY, {})["facility_points"] = {
            facility_id: {"point_m": moved}
        }
        shifted = effective_geometry(world).facility_points[facility_id]
        assert (shifted.x, shifted.y) == (moved["x"], moved["y"])
        del world.model_state[GEOMETRY_OVERLAY_KEY]["facility_points"]
        assert effective_geometry(world) == base


def test_tick_phases_reuse_parsed_geometry_without_changing_results(scenario, monkeypatch):
    def run(scoped):
        world = WorldState.from_scenario(scenario)
        initialize_simulator_extensions(world)
        controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
        handler = controller if scoped else controller._run_phase
        engine = TickEngine(world, phase_handler=handler)
        calls = counting(monkeypatch, FacilityGeometry)
        for _ in range(10):
            engine.step()
        monkeypatch.undo()
        return world.canonical_json(), len(calls)

    scoped_world, scoped_parses = run(True)
    unscoped_world, unscoped_parses = run(False)
    assert scoped_world == unscoped_world
    assert scoped_parses * 3 < unscoped_parses
