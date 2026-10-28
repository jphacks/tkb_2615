"""Experiments on the Project panel, from a generated Run: an approved plan prepares one
Run per cell, explicit resumes compute them, and the collected results feed an
evaluation and a calibration fit. A rerun of a saved Run reproduces its world.

These are the requests project-round3.js sends.
"""

from __future__ import annotations

import copy
import json

import pytest

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.web.projects import ProjectAPI

END = 30
PLAN = {
    "seeds": [1, 2],
    "variants": [{"id": "base"}, {"id": "fifo", "policies": {"queue": "strict-fifo-queue-v1"}}],
}
DATASET = {
    "source_id": "SYN-STRESS",
    "url": "https://example.org/synthetic-stress",
    "title": "synthetic stress level",
    "unit": "normalized_0_1",
    "lower": 0,
    "upper": 1,
    "applicability": {},
    "model_parameter_links": [],
}


@pytest.fixture
def project_api(tmp_path, monkeypatch, generated_previews, saved_timeline):
    """A ProjectAPI over Run r of the generated Scenario, not yet computed."""
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    preview = generated_previews(duration_days=END / 1440)
    scenario = ScenarioSpec.model_validate(copy.deepcopy(preview["scenario"]))
    store, _ = saved_timeline(tmp_path / "projects", scenario)
    api = ProjectAPI(store)
    try:
        yield api
    finally:
        api.close()


def _compute(api: ProjectAPI, run_id: str) -> None:
    api.post(["p", "runs", run_id, "resume"], {"branch_id": "main", "target_minute": END})
    # The worker is single; this waits for the resume queued above (#143).
    api._pool.submit(lambda: None).result(timeout=120)
    assert api.get(["p", "runs", run_id], {})["status"] == "completed"


def _run_experiment(api: ProjectAPI, purpose: str, observations: list[dict]) -> dict:
    run = api.get(["p", "runs", "r"], {})
    record = api.post(
        ["p", "experiments", "exp"],
        {
            "scenario_id": run["scenario_id"],
            "scenario_version": run["scenario_version"],
            "purpose": purpose,
            "plan": PLAN,
            "observations": observations,
        },
    )
    approval = {"approved": True, "approved_plan_hash": record["plan_hash"]}
    for cell in record["cells"]:
        prepared = api.post(
            ["p", "experiments", "exp", "cells", cell["cell_id"], "prepare"], approval
        )
        assert prepared["status"] == "prepared_not_executed"
        _compute(api, prepared["run_id"])
        api.post(["p", "experiments", "exp", "cells", cell["cell_id"], "collect"], {})
    return record


def test_experiment_cells_run_their_variant_and_feed_an_evaluation(project_api):
    api = project_api
    record = _run_experiment(api, "model_comparison", [])

    assert [(cell["variant_id"], cell["seed"]) for cell in record["cells"]] == [
        ("base", 1),
        ("base", 2),
        ("fifo", 1),
        ("fifo", 2),
    ]
    results = api.get(["p", "experiments", "exp", "results"], {"limit": ["100"]})["items"]
    assert [row["status"] for row in results] == ["recorded_not_verified"] * 4
    assert all(row["evidence"]["metric_minute"] == END - 1 for row in results)

    api.post(["p", "experiments", "exp", "finalize"], {"evaluation_id": "eval"})
    runs = api.get(["p", "evaluations", "eval"], {"section": ["runs"], "limit": ["100"]})["items"]
    queue = {row["variant_id"]: row["model_configs"]["policy_registry"]["queue"] for row in runs}
    assert queue == {"base": "need-weighted-physical-queue-v1", "fifo": "strict-fifo-queue-v1"}
    (comparison,) = api.get(
        ["p", "evaluations", "eval"], {"section": ["comparisons"], "limit": ["100"]}
    )["items"]
    assert (comparison["baseline_id"], comparison["candidate_id"]) == ("base", "fifo")


def test_calibration_fit_picks_the_variant_closest_to_the_observation(project_api):
    api = project_api
    api.post(["p", "datasets", "synthetic"], DATASET)
    observation = {"dataset_id": "synthetic", "metric": "stress_mean", "value": 0.3, "scale": 0.1}
    _run_experiment(api, "calibration", [observation])
    results = api.get(["p", "experiments", "exp", "results"], {"limit": ["100"]})["items"]

    fit = api.post(["p", "experiments", "exp", "fit"], {})

    def error(variant: str) -> float:
        values = [row["metrics"]["stress_mean"] for row in results if row["variant_id"] == variant]
        return sum(((value - 0.3) / 0.1) ** 2 for value in values) / len(values)

    expected = min(("base", "fifo"), key=error)
    assert api.get(["p", "experiments", "exp", "fit"], {})["candidate"]["variant_id"] == expected
    scores = {row["variant_id"]: row["training_score"] for row in fit["scores"]}
    assert scores == pytest.approx({variant: error(variant) for variant in scores})


def test_rerun_of_a_saved_run_reproduces_its_world(project_api):
    api = project_api
    _compute(api, "r")

    api.post(["p", "runs", "r", "rerun"], {"new_run_id": "r2"})
    _compute(api, "r2")

    def world(run_id: str) -> str:
        document = TimelineStore(api.store, "p", run_id).seek("main", END).world.to_dict()
        return json.dumps(document, sort_keys=True)

    assert world("r2") == world("r")
