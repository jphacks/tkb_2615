"""Checkpoint forks, intervention validation, and paired reproducibility."""

from __future__ import annotations

from pathlib import Path

import pytest

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine, TickPhase
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import (
    AddStaffCommand,
    AddToiletCommand,
    BranchError,
    BranchRuntime,
    SetFacilityWorkingUnitsCommand,
    command_from_dict,
)
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenarios" / "baseline_100_7d.json"


def test_noop_checkpoint_forks_are_independent_and_replay_identically() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    checkpoint = Checkpoint.capture(world, metrics=MetricsRecorder())
    left = BranchRuntime.from_checkpoint(checkpoint, branch_id="left")
    right = BranchRuntime.from_checkpoint(checkpoint, branch_id="right")

    assert left.world is not right.world
    assert left.world.agents["A001"] is not right.world.agents["A001"]
    assert left.pairing_key == right.pairing_key
    assert CounterRNG(left.world.seed).random("agent", "A001", 12) == CounterRNG(
        right.world.seed
    ).random("agent", "A001", 12)

    for branch in (left, right):

        def capture_metrics(phase, state, minute, rng):
            del rng
            if phase is TickPhase.COLLECT_METRICS:
                branch.metrics.capture(state, minute=minute)

        TickEngine(branch.world, phase_handler=capture_metrics).run(steps=10)

    assert left.world.canonical_json() == right.world.canonical_json()
    assert left.world.event_log.records == right.world.event_log.records
    assert left.metrics.to_dict() == right.metrics.to_dict()


def test_typed_interventions_are_logged_and_do_not_leak_between_branches() -> None:
    parent = BranchRuntime.from_world(WorldState.from_scenario(ScenarioSpec.load(FIXTURE)))
    with_staff = parent.fork("with-staff")
    with_toilet = parent.fork("with-toilet")

    staff_receipt = with_staff.apply(AddStaffCommand("staff-extra", "sleeping"))
    toilet_receipt = with_toilet.apply(
        AddToiletCommand("toilet-extra", "toilet", capacity=2, working_units=1)
    )
    disable_receipt = with_toilet.apply(
        SetFacilityWorkingUnitsCommand("toilet-extra", working_units=0)
    )

    assert "staff-extra" in with_staff.world.staff
    assert "staff-extra" not in with_toilet.world.staff
    assert "staff-extra" not in parent.world.staff
    assert with_toilet.world.facilities["toilet-extra"].working_units == 0
    assert len(with_staff.world.event_log.records) == 1
    assert len(with_toilet.world.event_log.records) == 2
    assert staff_receipt.state_hash != toilet_receipt.state_hash
    assert toilet_receipt.event_id != disable_receipt.event_id
    assert all(
        record.event.kind.value == "intervention" and record.status.value == "applied"
        for record in (*with_staff.world.event_log.records, *with_toilet.world.event_log.records)
    )


def test_rejected_intervention_is_atomic_and_command_parser_is_strict() -> None:
    runtime = BranchRuntime.from_world(WorldState.from_scenario(ScenarioSpec.load(FIXTURE)))
    original = runtime.world.canonical_json()

    with pytest.raises(BranchError, match="already exists"):
        runtime.apply(AddStaffCommand("S01", "sleeping"))
    with pytest.raises(BranchError, match="unknown facility"):
        runtime.apply(SetFacilityWorkingUnitsCommand("missing", 0))
    with pytest.raises(BranchError, match="unsupported"):
        command_from_dict({"kind": "add_staff", "staff_id": "x", "zone_id": "sleeping", "extra": 1})

    assert runtime.world.canonical_json() == original
    assert runtime.command_log == []


def test_branch_checkpoint_restores_lineage_and_interventions() -> None:
    root = BranchRuntime.from_world(WorldState.from_scenario(ScenarioSpec.load(FIXTURE)))
    child = root.fork("staff-branch")
    child.apply(AddStaffCommand("staff-extra", "sleeping"))

    restored = BranchRuntime.from_checkpoint(child.checkpoint(), branch_id="restored")

    assert restored.parent_branch_id == "staff-branch"
    assert restored.pairing_key == child.pairing_key
    assert restored.world.canonical_json() == child.world.canonical_json()
    assert restored.command_log == child.command_log
