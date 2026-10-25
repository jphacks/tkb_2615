"""Full checkpoint save/restore, compatibility, and deterministic continuation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.core.checkpoint import Checkpoint, CheckpointError, CheckpointSchedule
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
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenarios" / "failure_day3.json"


def test_paired_restore_constructs_one_validated_detached_world(monkeypatch) -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    metrics = MetricsRecorder()
    metrics.capture(world, minute=0)
    checkpoint = Checkpoint.capture(world, metrics=metrics)
    original = WorldState.from_dict.__func__
    calls = []

    def restore(cls, document):
        calls.append(document["minute_index"])
        return original(cls, document)

    monkeypatch.setattr(WorldState, "from_dict", classmethod(restore))
    restored_world, restored_metrics = checkpoint.restore_state()
    assert calls == [0]
    assert restored_world.canonical_json() == world.canonical_json()
    assert restored_metrics.to_dict() == metrics.to_dict()
    restored_world.metric_state["test_only"] = 1.0
    restored_metrics.snapshots.clear()
    again_world, again_metrics = checkpoint.restore_state()
    assert "test_only" not in again_world.metric_state
    assert again_metrics.to_dict() == metrics.to_dict()


def load_live_world() -> WorldState:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    toilet = ToiletServiceManager(world)
    toilet.request("A001", "toilet_1")
    toilet.request("A002", "toilet_1")
    toilet.arbitrate(0, CounterRNG(world.seed))
    staff = StaffService(world)
    staff.request("A003")
    staff.assign_pending(0)
    world.model_state = {"behavior-model": {"adaptive_state": [0.25, 0.75]}}
    world.metric_state["model_accumulator"] = 12.0

    audit_event = ScheduledEvent(
        event_id="checkpoint-audit",
        kind=EventKind.EXOGENOUS,
        due_minute=0,
        priority=0,
        sequence=world.allocate_event_sequence(),
        event_type="unknown_checkpoint_test_event",
    )
    world.schedule_event(audit_event)
    EventDispatcher().dispatch_due(world, 0)
    world.validate()
    assert any(task.kind == "toilet_service" for task in world.active_tasks.values())
    assert any(task.kind == "toilet_queue" for task in world.active_tasks.values())
    assert any(task.kind == "staff_support" for task in world.active_tasks.values())
    assert world.event_queue.pending()
    assert world.event_log.records
    return world


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


def test_checkpoint_has_independent_schema_and_captures_every_runtime_state(tmp_path: Path) -> None:
    world = load_live_world()
    metrics = MetricsRecorder()
    metrics.capture(world, minute=0)
    CheckpointSchedule(interval_minutes=30)

    checkpoint = Checkpoint.capture(
        world,
        metrics=metrics,
        runtime_state={"decision_tape_cursor": 7, "policy_state": {"recent": ["x"]}},
    )
    path = tmp_path / "minute-0.checkpoint.json"
    checkpoint.save(path)
    restored_checkpoint = Checkpoint.load(path)
    restored = restored_checkpoint.restore_world(
        expected_model_versions=restored_checkpoint.model_versions
    )

    assert checkpoint.schema_version == restored_checkpoint.schema_version == 1
    assert checkpoint.world_state_schema_version == world.schema_version == 5
    assert restored.canonical_json() == world.canonical_json()
    assert restored.event_queue.pending() == world.event_queue.pending()
    assert restored.event_log.records == world.event_log.records
    assert restored.active_tasks == world.active_tasks
    assert restored.rng_state == world.rng_state
    assert restored.model_state == world.model_state
    assert restored_checkpoint.runtime_state == {
        "decision_tape_cursor": 7,
        "policy_state": {"recent": ["x"]},
    }
    assert restored_checkpoint.restore_metrics().to_dict() == metrics.to_dict()


def test_checkpoint_restore_then_uninterrupted_run_is_fully_identical() -> None:
    world = load_live_world()
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


def test_checkpoint_fails_closed_for_versions_corruption_and_model_mismatch() -> None:
    checkpoint = Checkpoint.capture(load_live_world())
    document = checkpoint.to_dict()

    old_checkpoint = dict(document, schema_version=2)
    with pytest.raises(CheckpointError, match="checkpoint schema"):
        Checkpoint.from_dict(old_checkpoint)

    extended_checkpoint = dict(document, unexpected_contract_field=True)
    with pytest.raises(CheckpointError, match="fields do not match"):
        Checkpoint.from_dict(extended_checkpoint)

    corrupted = json.loads(checkpoint.to_json())
    corrupted["world_state"]["resources"]["water"] = 500
    with pytest.raises(CheckpointError, match="hash mismatch"):
        Checkpoint.from_dict(corrupted)

    wrong_world_version = dict(document, world_state_schema_version=4)
    with pytest.raises(CheckpointError, match="WorldState schema version"):
        Checkpoint.from_dict(wrong_world_version)

    with pytest.raises(CheckpointError, match="model versions"):
        checkpoint.restore_world(expected_model_versions={"behavior": "unexpected"})


def test_checkpoint_cadence_is_configurable_and_defaults_to_thirty_minutes() -> None:
    schedule = CheckpointSchedule()

    assert schedule.interval_minutes == 30
    assert schedule.should_capture(0)
    assert schedule.should_capture(60)
    assert not schedule.should_capture(59)
    assert CheckpointSchedule(10).should_capture(20)
