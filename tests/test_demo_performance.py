"""Focused response-time and replay-safety regressions for the local demo."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import UtilityBehaviorPolicy, WorldView
from sheltersim.policies.psychology import SimplePsychologyPolicy
from sheltersim.web.demo import DemoService
from sheltersim.web.jobs import DemoJobManager

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenarios" / "baseline_100_7d.json"


def _wait_for_completion(manager: DemoJobManager, job_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job["status"] in {"completed", "failed", "cancelled", "interrupted"}:
            return job
        time.sleep(0.01)
    pytest.fail(f"job {job_id} did not complete: {manager.get(job_id)}")


def test_psychology_view_skips_audit_hash_without_changing_inputs() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    before = world.canonical_json()
    hashed = WorldView.from_world(world)
    psychology_view = WorldView.from_world(world, include_state_hash=False)

    assert hashed.state_hash is not None
    assert psychology_view.state_hash is None
    assert psychology_view.agents == hashed.agents
    assert psychology_view.facilities == hashed.facilities
    policy = SimplePsychologyPolicy()
    assert policy.explain_update(hashed, 0) == policy.explain_update(psychology_view, 0)
    assert world.canonical_json() == before


def test_behavior_refuses_a_view_without_its_required_audit_hash() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    view = WorldView.from_world(world, include_state_hash=False)

    with pytest.raises(ValueError, match="require a WorldView with a state hash"):
        UtilityBehaviorPolicy().propose(view, next(iter(world.agents)), CounterRNG(world.seed))


def test_live_recompute_progress_and_noop_branch_checkpoint_are_reproducible(
    tmp_path: Path,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["duration_minutes"] = 12
    document["scheduled_events"] = []
    demo = DemoService(ScenarioSpec.model_validate(document))
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    demo.dispatch("fork", {"branch_id": "baseline", "new_branch_id": "no-op"})
    manager = DemoJobManager(demo, tmp_path, chunk_minutes=4)
    try:
        baseline_job = manager.submit("baseline", 12)
        replay_job = manager.submit("no-op", 12)
        assert baseline_job["manifest"] == {
            "schema_version": 1,
            "execution_mode": "live_compute",
            "scenario_id": demo.scenario.id,
            "seed": demo.scenario.seed,
            "agent_count": 100,
            "duration_minutes": 12,
            "scenario_source": "packaged_or_local_scenario",
            "scenario_hash": demo.state()["scenario_hash"],
            "scenario_family_hash": demo.state()["scenario_family_hash"],
            "run_id": demo.state()["run_id"],
        }

        baseline = _wait_for_completion(manager, baseline_job["job_id"])
        replay = _wait_for_completion(manager, replay_job["job_id"])
        assert baseline["status"] == replay["status"] == "completed"
        assert baseline["percent"] == replay["percent"] == 100

        baseline_branch = demo.branches["baseline"]
        replay_branch = demo.branches["no-op"]
        assert (
            baseline_branch.runtime.world.canonical_json()
            == replay_branch.runtime.world.canonical_json()
        )
        assert (
            baseline_branch.runtime.world.event_queue.pending()
            == replay_branch.runtime.world.event_queue.pending()
        )
        assert baseline_branch.runtime.world.model_state.get("resource_ledger_v1") == (
            replay_branch.runtime.world.model_state.get("resource_ledger_v1")
        )
        assert baseline_branch.checkpoints[
            next(reversed(baseline_branch.checkpoints))
        ].state_hash == (
            replay_branch.checkpoints[next(reversed(replay_branch.checkpoints))].state_hash
        )
    finally:
        manager.close()
