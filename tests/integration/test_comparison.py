"""Comparisons of saved Runs: the first metric change with its event IDs, and run sets
paired by seed.

Reports built from Branches in memory are checked in
tests/unit/experiments/test_comparison.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from sheltersim.__main__ import main
from sheltersim.core.world import WorldState
from sheltersim.experiments.comparison import (
    compare_run_artifacts,
    compare_run_sets,
)
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter

FIXTURE = Path(__file__).parents[2] / "fixtures" / "scenario_100_agents.json"


def test_saved_run_comparison_uses_first_metric_change_and_event_ids(
    tmp_path: Path, capsys
) -> None:
    scenario = ScenarioSpec.model_validate(
        json.loads(FIXTURE.read_text(encoding="utf-8")) | {"duration_minutes": 3}
    )
    outputs = []
    for name, changed in (("baseline", False), ("candidate", True)):
        world = WorldState.from_scenario(scenario)
        if changed:
            world.facilities["toilet_1"].working_units = 0
        writer = RunArtifactWriter(scenario, tmp_path / name)
        writer.write_initial(world)
        writer.capture_metric(world, minute=0)
        world.minute_index = 1
        writer.capture_metric(world, minute=1)
        writer.finalize(world)
        outputs.append(tmp_path / name)

    report = compare_run_artifacts(*outputs)

    assert report["first_divergence_minute"] == 0
    assert report["final_metric_differences"]["toilet_working_units"]["delta"] == -1
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


def test_run_sets_pair_by_seed_and_report_paired_distribution(tmp_path: Path, capsys) -> None:
    baseline_runs = []
    candidate_runs = []
    for seed in (11, 22, 33):
        scenario = ScenarioSpec.model_validate(
            json.loads(FIXTURE.read_text(encoding="utf-8")) | {"duration_minutes": 3, "seed": seed}
        )
        for side, changed, destination in (
            ("baseline", False, baseline_runs),
            ("candidate", True, candidate_runs),
        ):
            world = WorldState.from_scenario(scenario)
            if changed:
                world.facilities["toilet_1"].working_units = 0
            run_path = tmp_path / f"{side}-{seed}"
            writer = RunArtifactWriter(scenario, run_path)
            writer.write_initial(world)
            writer.capture_metric(world, minute=0)
            writer.finalize(world)
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


