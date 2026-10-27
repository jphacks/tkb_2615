"""A Run resumed from a Checkpoint continues exactly as the uninterrupted Run.

The Checkpoint's schema, save and load, and the versions and hashes it refuses
are checked in tests/unit/core/test_checkpoint.py.
"""

from __future__ import annotations

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine, TickPhase
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.staff import StaffService
from sheltersim.events.handlers import (
    EventDispatcher,
    FacilityOutageHandler,
    StaffSupportCompletionHandler,
    SupportRequestTimeoutHandler,
    ToiletServiceCompletionHandler,
)
from sheltersim.experiments.metrics import MetricsRecorder


def make_phase_handler(world: WorldState, metrics: MetricsRecorder):
    toilet = ToiletServiceManager(world)
    staff = StaffService(world)
    dispatcher = EventDispatcher(
        {
            "facility_outage": FacilityOutageHandler(),
            "toilet_service_complete": ToiletServiceCompletionHandler(),
            "staff_support_complete": StaffSupportCompletionHandler(),
            "support_request_timeout": SupportRequestTimeoutHandler(),
        }
    )

    def handle(phase: TickPhase, state: WorldState, minute: int, rng: CounterRNG) -> None:
        if phase is TickPhase.DISPATCH_DUE_EVENTS:
            dispatcher.dispatch_due(state, minute)
        elif phase is TickPhase.ARBITRATE_ACTIONS:
            toilet.arbitrate(minute, rng, validate_state=False)
            staff.assign_pending(minute, validate_state=False)
        elif phase is TickPhase.COLLECT_METRICS:
            metrics.capture(state, minute=minute)

    return handle


def test_checkpoint_restore_then_uninterrupted_run_is_fully_identical(
    live_checkpoint_world,
) -> None:
    world = live_checkpoint_world
    metrics = MetricsRecorder()
    engine = TickEngine(world, phase_handler=make_phase_handler(world, metrics))
    engine.run(steps=3)

    checkpoint = Checkpoint.capture(
        world,
        metrics=metrics,
        runtime_state={"decision_tape_cursor": 11},
    )
    resumed_world = checkpoint.restore_world()
    resumed_metrics = checkpoint.restore_metrics()
    uninterrupted_engine = TickEngine(
        world,
        phase_handler=make_phase_handler(world, metrics),
    )
    resumed_engine = TickEngine(
        resumed_world,
        phase_handler=make_phase_handler(resumed_world, resumed_metrics),
    )

    uninterrupted_engine.run(steps=45)
    resumed_engine.run(steps=45)

    assert resumed_world.canonical_json() == world.canonical_json()
    assert resumed_metrics.to_dict() == metrics.to_dict()
    assert checkpoint.world_state_schema_version == 5
    assert checkpoint.schema_version == 1
