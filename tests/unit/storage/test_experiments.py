"""Preparing an experiment cell stays offline: no live provider, and no Run is launched."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support.paths import SMALL_REPLAY

from sheltersim.storage.projects import ProjectStore, digest


def test_experiment_plan_and_cell_preparation_never_launch_or_construct_live_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.llm.provider import AnthropicProvider
    from sheltersim.storage.experiments import ExperimentStore
    from sheltersim.web.projects import ProjectAPI

    store = ProjectStore(tmp_path)
    store.create_project("p")
    scenario = ScenarioSpec.load(SMALL_REPLAY)
    saved = store.save_scenario(
        "p", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
    )
    experiments = ExperimentStore(store)
    plan = experiments.create(
        "p",
        "matrix",
        {
            "scenario_id": scenario.id,
            "scenario_version": saved["scenario_version"],
            "plan": {
                "seeds": [1, 2],
                "variants": [
                    {"id": "base"},
                    {"id": "other", "parameters": {"psychology.stress_pressure": 0.008}},
                ],
            },
        },
    )
    assert len(plan["cells"]) == 4
    assert store.runs("p") == []
    assert plan["external_validation"] is False

    def no_live(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("scientific cell preparation must remain offline and unexecuted")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "unit-placeholder-not-a-real-key")
    monkeypatch.setattr(AnthropicProvider, "__init__", no_live)
    api = ProjectAPI(store, recover=False)
    monkeypatch.setattr(api._pool, "submit", no_live)
    try:
        result = experiments.prepare(
            api,
            "p",
            "matrix",
            "cell-0000",
            {"approved": True, "approved_plan_hash": plan["plan_hash"]},
        )
        _, checkpoint = store.load_branch("p", result["run_id"], "main")
        assert checkpoint.simulation_minute == 0
        assert checkpoint.runtime_state["execution_purpose"] == "offline_experiment"
        assert checkpoint.runtime_state["llm"]["mode"] == "offline"
        retry = experiments.prepare(
            api,
            "p",
            "matrix",
            "cell-0000",
            {"approved": True, "approved_plan_hash": plan["plan_hash"]},
        )
        assert retry["scenario_version"] == result["scenario_version"]
        assert len(store.runs("p")) == 1
    finally:
        api.close()
