"""A Run resumed from a Checkpoint continues exactly as the uninterrupted Run.

On the shared generated Scenario, so the Checkpoint holds what a Run made on the
Project screen holds: people and staff walking on the floor plan (movement_v1),
queues at service points, and toilets an intervention placed on the floor plan
(geometry_overlay_v1). Until #137 this ran on failure_day3, which has neither a
floor plan nor facility awareness.

The Checkpoint's schema, save and load, and the versions and hashes it refuses
are checked in tests/unit/core/test_checkpoint.py.
"""

from __future__ import annotations

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.domain.geometry_overlay import GEOMETRY_OVERLAY_KEY
from sheltersim.domain.movement import MOVEMENT_STATE_KEY
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink

INSTALL_MINUTE = 20
# People walk to and queue at the toilets, and staff walk to a person, at this minute.
CHECKPOINT_MINUTE = 40


def _engine(world: WorldState, metrics: MetricsRecorder) -> TickEngine:
    """The controller the Project screen runs, recording metrics into metrics."""
    return TickEngine(world, phase_handler=OfflineSimulationController(world, _MetricSink(metrics)))


def test_checkpoint_restore_then_uninterrupted_run_is_fully_identical(
    generated_scenario, add_toilets_program
) -> None:
    runtime = BranchRuntime.from_world(WorldState.from_scenario(generated_scenario))
    runtime.apply_effect_program(add_toilets_program(start_minute=INSTALL_MINUTE))
    world, metrics = runtime.world, runtime.metrics
    uninterrupted = _engine(world, metrics)
    uninterrupted.run(steps=CHECKPOINT_MINUTE)

    assert world.model_state[MOVEMENT_STATE_KEY]["journeys"]
    assert world.model_state[GEOMETRY_OVERLAY_KEY]["facility_points"]
    assert {"toilet_queue", "staff_support_travel"} <= {
        task.kind for task in world.active_tasks.values()
    }

    # Through JSON, as the Project screen saves and loads it.
    checkpoint = Checkpoint.from_json(Checkpoint.capture(world, metrics=metrics).to_json())
    resumed_world, resumed_metrics = checkpoint.restore_state()

    uninterrupted.run(steps=60)
    _engine(resumed_world, resumed_metrics).run(steps=60)

    assert resumed_world.canonical_json() == world.canonical_json()
    assert resumed_metrics.to_dict() == metrics.to_dict()
