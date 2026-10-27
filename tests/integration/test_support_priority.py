"""Offline checks for bounded support-priority requests and legacy replay.

That each CLI run opens its own usage account is checked in tests/e2e/test_cli_run.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from sheltersim.core.engine import TickEngine
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.experiments.timeline import TimelineStore, _MetricSink
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import LLMCallRecord, ProviderResponse
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.runtime import (
    SUPPORT_PRIORITY_STATE_KEY,
    SupportPriorityLimits,
    support_priority_policy_version,
)
from sheltersim.storage.projects import ProjectStore, digest


class RecordingProvider:
    """Accept every task and keep the tasks for inspection."""

    def __init__(self) -> None:
        self.tasks = []

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        del max_output_tokens, timeout_seconds
        self.tasks.append(task)
        properties = task.output_schema["properties"]
        if "priority" in properties:
            text = '{"priority":1,"rationale":"synthetic urgent"}'
        elif set(properties) == {"action"}:
            text = '{"action":"offer_reassurance"}'
        else:
            text = task.fallback_text
        return ProviderResponse(
            text=text, model_id=model_id, input_tokens=10, output_tokens=5, elapsed_ms=0.0
        )


def _router(provider) -> LLMRouter:
    settings = LLMSettings(mode=LLMMode.RECORD, api_key="mock-key", max_api_calls=1_000)
    return LLMRouter(settings, provider=provider, tape=ResponseTape())


def _run_controller(world: WorldState, router: LLMRouter, *, steps: int) -> None:
    controller = OfflineSimulationController(
        world, _MetricSink(MetricsRecorder()), llm_router=router
    )
    TickEngine(world, phase_handler=controller, rng=CounterRNG(world.seed)).run(steps=steps)


def _priority_tasks(provider: RecordingProvider) -> list:
    return [task for task in provider.tasks if task.prompt_version.startswith("support-priority")]


def _legacy_record() -> LLMCallRecord:
    return LLMCallRecord(
        call_index=0,
        stage="runtime",
        outcome="recorded",
        model_id="claude-haiku-5-5",
        input_hash="legacy-input",
        state_hash="legacy-state",
        prompt_version="support-priority-v1",
        schema_version=1,
        api_request_count=1,
        input_tokens=10,
        output_tokens=5,
        estimated_cost_usd=0.0,
        elapsed_ms=0.0,
        cache_hit=False,
        fallback_used=False,
    )


def test_v2_asks_only_stressed_requesters_within_the_daily_cap(
    tmp_path: Path, small_benchmark_scenario
) -> None:
    provider = RecordingProvider()
    world = run_scenario(
        small_benchmark_scenario(300), tmp_path / "run", llm_router=_router(provider)
    )

    limits = SupportPriorityLimits()
    tasks = _priority_tasks(provider)
    assert 0 < len(tasks) <= limits.global_per_day
    for task in tasks:
        prompt = json.loads(task.input_text)
        assert task.prompt_version == "support-priority-v2"
        assert prompt["stress"] >= limits.stress_threshold
        assert "at most 80 characters" in " ".join(prompt["constraints"])
    state = world.model_state[SUPPORT_PRIORITY_STATE_KEY]
    assert state["attempts_by_day"] == {"0": len(tasks)}
    assert state["skipped_by_reason"]["below_stress_threshold"] > 0
    # Social proposals are no longer starved by per-request priority calls.
    assert any(task.prompt_version.startswith("event-triggered-social") for task in provider.tasks)


def test_daily_cap_resets_on_the_next_simulated_day(
    tmp_path: Path, small_benchmark_scenario
) -> None:
    provider = RecordingProvider()
    world = run_scenario(
        small_benchmark_scenario(1_560), tmp_path / "run", llm_router=_router(provider)
    )

    state = world.model_state[SUPPORT_PRIORITY_STATE_KEY]
    assert state["attempts_by_day"]["0"] == SupportPriorityLimits().global_per_day
    assert state["attempts_by_day"]["1"] >= 1
    assert state["skipped_by_reason"]["global_daily_cap"] > 0


def test_offline_runs_create_no_support_priority_state(
    tmp_path: Path, small_benchmark_scenario
) -> None:
    world = run_scenario(small_benchmark_scenario(200), tmp_path / "run")

    assert SUPPORT_PRIORITY_STATE_KEY not in world.model_state


def test_histories_recorded_under_v1_keep_the_v1_policy(small_benchmark_scenario) -> None:
    fresh = _router(RecordingProvider())
    assert support_priority_policy_version(fresh) == "support-priority-v2"

    provider = RecordingProvider()
    legacy = _router(provider)
    legacy.records.append(_legacy_record())
    assert support_priority_policy_version(legacy) == "support-priority-v1"

    world = WorldState.from_scenario(small_benchmark_scenario(30))
    _run_controller(world, legacy, steps=30)

    tasks = _priority_tasks(provider)
    # v1 asked about every pending request at minute 0, with the original prompt.
    assert tasks and {task.prompt_version for task in tasks} == {"support-priority-v1"}
    assert all(
        "at most 80 characters" not in " ".join(json.loads(task.input_text)["constraints"])
        for task in tasks
    )
    assert SUPPORT_PRIORITY_STATE_KEY not in world.model_state


def test_v2_project_history_replays_exactly_without_a_provider(
    tmp_path: Path, small_benchmark_scenario
) -> None:
    scenario = small_benchmark_scenario(200)
    store = ProjectStore(tmp_path)
    store.create_project("project")
    saved = store.save_scenario(
        "project", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
    )
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    runtime = BranchRuntime.from_world(WorldState.from_scenario(scenario))
    router = _router(RecordingProvider())
    timeline = TimelineStore(store, "project", "run")
    timeline.record(runtime, router=router)
    live = timeline.advance("main", 200, router=router)
    assert live.world.model_state[SUPPORT_PRIORITY_STATE_KEY]["attempts_by_day"]["0"] > 0

    # Seeking between checkpoints reconstructs v2 decisions from the recorded tape only.
    replayed = timeline.seek("main", 195)
    reference = WorldState.from_scenario(scenario)
    _run_controller(reference, _router(RecordingProvider()), steps=195)
    assert replayed.world.canonical_json() == reference.canonical_json()
