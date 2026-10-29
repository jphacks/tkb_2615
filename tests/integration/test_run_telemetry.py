"""Measuring a Run on the Project screen: "計算" with measurement records each execution,
"画面FPS" saves a render report, and a malformed report is refused with a reason.

Measurements live beside the Run, never in its Checkpoints.
"""

from __future__ import annotations

import copy

import pytest

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.projects import StorageError
from sheltersim.web.projects import ProjectAPI

END = 60
RENDER = {
    "kind": "render",
    "branch_id": "main",
    "samples": 120,
    "fps": 58.5,
    "p95_frame_ms": 21.0,
    "visibility": "visible",
}


@pytest.fixture
def run_api(tmp_path, monkeypatch, generated_previews, saved_timeline):
    """A ProjectAPI over Run r of a one-hour generated Scenario, not yet computed."""
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    preview = generated_previews(duration_days=END / 1440)
    scenario = ScenarioSpec.model_validate(copy.deepcopy(preview["scenario"]))
    store, _ = saved_timeline(tmp_path / "projects", scenario)
    api = ProjectAPI(store)
    try:
        yield api
    finally:
        api.close()


def _compute(api: ProjectAPI, minute: int, *, measure: bool) -> None:
    api.post(
        ["p", "runs", "r", "resume"],
        {"branch_id": "main", "target_minute": minute, "measure_performance": measure},
    )
    # The worker is single; this waits for the resume queued above (#143).
    api.wait_idle(timeout=120)


def test_measured_computations_are_recorded_beside_the_run(run_api):
    empty = run_api.get(["p", "runs", "r", "telemetry"], {})
    _compute(run_api, 20, measure=True)
    _compute(run_api, 40, measure=False)
    _compute(run_api, END, measure=True)

    telemetry = run_api.get(["p", "runs", "r", "telemetry"], {})

    assert (empty["execution_count"], empty["executions"]) == (0, [])
    assert telemetry["execution_count"] == 2, "only the measured computations"
    executions = telemetry["executions"]
    assert [(row["from_minute"], row["computed_until"]) for row in executions] == [
        (0, 20),
        (40, 60),
    ]
    assert [row["status"] for row in executions] == ["paused", "completed"]
    assert all(row["wall_seconds"] > 0 and row["checkpoint_write_bytes"] > 0 for row in executions)
    # Runs share the server process, so their memory cannot be told apart.
    assert {row["memory_attribution_status"] for row in executions} == {"unavailable"}
    assert telemetry["totals"]["checkpoint_write_bytes"] == sum(
        row["checkpoint_write_bytes"] for row in executions
    )


def test_a_render_report_is_saved_for_its_branch(run_api):
    saved = run_api.post(["p", "runs", "r", "telemetry"], RENDER)

    (report,) = saved["render_reports"]
    assert {key: report[key] for key in RENDER} == RENDER
    assert run_api.get(["p", "runs", "r", "telemetry"], {}) == saved


@pytest.mark.parametrize(
    "change",
    [
        {"branch_id": "unknown"},
        {"samples": -1},
        {"samples": True},
        {"fps": float("nan")},
        {"fps": 1e9},
        {"fps": "60"},
        {"kind": "cpu"},
        {"visibility": "Mozilla/5.0 (X11; Linux x86_64)"},
        {"visibility": None},
        {"user_agent": "Mozilla/5.0"},
    ],
    ids=lambda change: next(iter(change)) + "=" + repr(next(iter(change.values()))),
)
def test_a_malformed_render_report_is_refused(run_api, change):
    with pytest.raises(StorageError):
        run_api.post(["p", "runs", "r", "telemetry"], RENDER | change)

    assert run_api.get(["p", "runs", "r", "telemetry"], {})["render_reports"] == []
