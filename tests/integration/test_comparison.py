"""Comparisons of saved Runs: the first metric change with its event IDs, and run sets
paired by seed.

On Runs of a generated Scenario made with run_scenario. A saved-run comparison
pairs Runs of one frozen Scenario family whose event schedules may differ, so the
candidate is the same Scenario with a toilet outage scheduled at minute 0. Until
#137 these tests set a toilet's working units by hand and wrote the metrics
without running the simulation, on scenario_100_agents.

Reports built from Branches in memory are checked in
tests/unit/experiments/test_comparison.py.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sheltersim.__main__ import main
from sheltersim.experiments.comparison import (
    compare_run_artifacts,
    compare_run_sets,
)
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

# 30 minutes: the outage shows from the first metric sample, and six Runs stay short.
DURATION_DAYS = 1 / 48
OUTAGE = {
    "id": "toilet-01-outage",
    "kind": "exogenous",
    "due_minute": 0,
    "priority": 0,
    "sequence": 0,
    "event_type": "facility_outage",
    "payload": {"facility_id": "toilet_01", "units_out": 1},
    "source_id": "TEST-COMPARISON-OUTAGE",
    "causation_id": None,
    "provenance": {
        "source_type": "assumption",
        "source_id": "TEST-COMPARISON-OUTAGE",
        "description": "Test input: one toilet is out of order from the start.",
    },
}


@pytest.fixture
def scenarios(generated_previews) -> Callable[[int | None], tuple[ScenarioSpec, ScenarioSpec]]:
    """Make the baseline and the candidate with the outage, optionally with another seed."""
    document = generated_previews(duration_days=DURATION_DAYS)["scenario"]

    def build(seed: int | None = None) -> tuple[ScenarioSpec, ScenarioSpec]:
        baseline: dict[str, Any] = copy.deepcopy(document)
        if seed is not None:
            baseline["seed"] = seed
        candidate = copy.deepcopy(baseline) | {"scheduled_events": [OUTAGE]}
        return ScenarioSpec.model_validate(baseline), ScenarioSpec.model_validate(candidate)

    return build


def test_saved_run_comparison_uses_first_metric_change_and_event_ids(
    tmp_path: Path, capsys, scenarios
) -> None:
    outputs = [tmp_path / "baseline", tmp_path / "candidate"]
    for scenario, output in zip(scenarios(), outputs, strict=True):
        run_scenario(scenario, output)

    report = compare_run_artifacts(*outputs)

    assert report["first_divergence_minute"] == 0
    assert report["changed_metrics_at_first_divergence"]["toilet_working_units"]["delta"] == -1
    assert report["final_metric_differences"]["toilet_working_units"]["delta"] == -1
    assert OUTAGE["id"] in report["added_event_ids"]
    (window,) = [
        item for item in report["event_windows"]["candidate"] if item["event_id"] == OUTAGE["id"]
    ]
    assert window["realized_status"] == "applied"
    assert window["source_id"] == OUTAGE["source_id"]
    output = tmp_path / "comparison.json"

    assert (
        main(
            [
                "compare",
                "--baseline",
                str(outputs[0]),
                "--candidate",
                str(outputs[1]),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.is_file()
    assert output.with_suffix(".md").is_file()
    assert json.loads(output.read_text(encoding="utf-8"))["first_divergence_minute"] == 0
    assert json.loads(capsys.readouterr().out)["report"] == str(output)


def test_run_sets_pair_by_seed_and_report_paired_distribution(
    tmp_path: Path, capsys, scenarios
) -> None:
    baseline_runs = []
    candidate_runs = []
    for seed in (11, 22, 33):
        for scenario, side, destination in zip(
            scenarios(seed),
            ("baseline", "candidate"),
            (baseline_runs, candidate_runs),
            strict=True,
        ):
            run_path = tmp_path / f"{side}-{seed}"
            run_scenario(scenario, run_path)
            destination.append(run_path)

    report = compare_run_sets(baseline_runs, candidate_runs)

    assert report["seed_count"] == 3
    assert report["seeds"] == [11, 22, 33]
    assert report["paired_delta_summary"]["toilet_working_units"]["mean"] == -1
    assert [item["seed"] for item in report["per_seed_reports"]] == [11, 22, 33]
    assert "not a guarantee" in report["interpretation"]
    output = tmp_path / "paired.json"
    assert (
        main(
            [
                "compare",
                "--baseline",
                *(str(path) for path in baseline_runs),
                "--candidate",
                *(str(path) for path in candidate_runs),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text(encoding="utf-8"))["seed_count"] == 3
    assert output.with_suffix(".md").is_file()
    capsys.readouterr()
