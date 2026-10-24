"""First-divergence reports, causal links, and paired-seed summaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.__main__ import main
from sheltersim.core.engine import TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import AddToiletCommand, BranchRuntime
from sheltersim.experiments.comparison import (
    build_causal_trace,
    compare_branches,
    compare_run_artifacts,
    compare_run_sets,
    summarize_seed_comparisons,
)
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


def test_noop_branch_report_has_zero_divergence() -> None:
    parent = BranchRuntime.from_world(WorldState.from_scenario(ScenarioSpec.load(FIXTURE)))
    parent.metrics.capture(parent.world, minute=0)
    left = parent.fork("left")
    right = parent.fork("right")

    report = compare_branches(left, right).to_dict()

    assert report["first_divergence_minute"] is None
    assert report["changed_agents"] == []
    assert report["changed_facilities"] == []
    assert report["added_event_ids"] == []
    assert report["no_op_equivalent"] is True


def test_intervention_report_explains_first_kpi_and_state_difference() -> None:
    parent = BranchRuntime.from_world(WorldState.from_scenario(ScenarioSpec.load(FIXTURE)))
    baseline = parent.fork("baseline")
    candidate = parent.fork("candidate")
    baseline.metrics.capture(baseline.world, minute=0)
    candidate.apply(AddToiletCommand("extra-toilet", "toilet", capacity=2))
    candidate.metrics.capture(candidate.world, minute=0)

    report = compare_branches(baseline, candidate).to_dict()

    assert report["first_divergence_minute"] == 0
    assert report["changed_metrics_at_first_divergence"]["toilet_working_units"]["delta"] == 2
    assert report["changed_facilities"][0]["id"] == "extra-toilet"
    assert report["added_event_ids"] == ["branch-intervention-0"]
    assert "single seed" in report["interpretation"]


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


def test_policy_comparison_requires_only_the_registered_variant_to_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_fixture = Path(__file__).parents[1] / "fixtures/scenarios/h00_small_replay.json"
    document = json.loads(policy_fixture.read_text(encoding="utf-8"))
    document["duration_minutes"] = 240
    document["profiles"] = document["profiles"][:12]
    document["model_configs"]["behavior"]["temperature"] = 2.0
    baseline = ScenarioSpec.from_json(json.dumps(document))
    candidate = baseline.with_behavior_policy_variant("priority-rule-v1")
    baseline_output = tmp_path / "softmax"
    candidate_output = tmp_path / "priority"
    run_scenario(baseline, baseline_output, trace_level=TraceLevel.FULL)
    run_scenario(candidate, candidate_output, trace_level=TraceLevel.FULL)

    with pytest.raises(ValueError, match="same frozen scenario family"):
        compare_run_artifacts(baseline_output, candidate_output)
    report = compare_run_artifacts(
        baseline_output,
        candidate_output,
        allow_behavior_policy_change=True,
    )

    assert report["behavior_policy_comparison"]["baseline_variant"] == "utility-softmax-v1"
    assert report["behavior_policy_comparison"]["candidate_variant"] == "priority-rule-v1"
    assert (
        report["behavior_policy_comparison"]["baseline_scenario_hash"]
        != (report["behavior_policy_comparison"]["candidate_scenario_hash"])
    )
    assert report["behavior_policy_comparison"]["baseline_run_id"]
    assert report["behavior_policy_comparison"]["candidate_run_id"]
    assert report["action_counts"]["baseline"]["available"] is True
    assert (
        report["action_counts"]["baseline"]["by_action"]
        != report["action_counts"]["candidate"]["by_action"]
    )
    assert report["final_metric_differences"]["stress_mean"]["delta"] != 0
    assert "not evidence of real-world" in report["interpretation"]
    support_cohort = report["need_cohort_comparison"]["cohorts"]["highest_support_need_quartile"]
    mobility_cohort = report["need_cohort_comparison"]["cohorts"]["mobility_support"]
    assert support_cohort["population"] == 1
    assert support_cohort["baseline"]["agents_with_recorded_needs"] > 0
    assert "hunger" in support_cohort["delta_candidate_minus_baseline"]
    assert mobility_cohort["population"] > 0
    assert "not minute-weighted" in report["need_cohort_comparison"]["measure"]

    comparison_exit = main(
        [
            "compare",
            "--baseline",
            str(baseline_output),
            "--candidate",
            str(candidate_output),
            "--behavior-policy-comparison",
        ]
    )
    assert comparison_exit == 0
    assert "need_cohort_comparison" in capsys.readouterr().out

    profile_path = candidate_output / "agents_initial.json"
    profiles = json.loads(profile_path.read_text(encoding="utf-8"))
    profiles["profiles"][0]["support_need"] = 1.0 - profiles["profiles"][0]["support_need"]
    profile_path.write_text(json.dumps(profiles), encoding="utf-8")
    with pytest.raises(ValueError, match="inputs differ"):
        compare_run_artifacts(
            baseline_output,
            candidate_output,
            allow_behavior_policy_change=True,
        )


def test_paired_seed_summary_reports_distribution_without_causal_claim() -> None:
    summary = summarize_seed_comparisons(
        [
            {"seed": 1, "final_metric_differences": {"stress_mean": {"delta": 0.1}}},
            {"seed": 2, "final_metric_differences": {"stress_mean": {"delta": 0.3}}},
            {"seed": 3, "final_metric_differences": {"stress_mean": {"delta": 0.2}}},
        ]
    )

    assert summary["seed_count"] == 3
    assert summary["paired_delta_summary"]["stress_mean"]["mean"] == pytest.approx(0.2)
    assert summary["paired_delta_summary"]["stress_mean"]["median"] == pytest.approx(0.2)
    assert summary["paired_delta_summary"]["stress_mean"]["p95"] > 0.2
    assert "not a guarantee" in summary["interpretation"]


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


def test_run_sets_reject_duplicate_or_unpaired_seeds(tmp_path: Path) -> None:
    scenario = ScenarioSpec.model_validate(
        json.loads(FIXTURE.read_text(encoding="utf-8")) | {"duration_minutes": 3}
    )
    world = WorldState.from_scenario(scenario)
    writer = RunArtifactWriter(scenario, tmp_path / "run")
    writer.write_initial(world)
    writer.capture_metric(world, minute=0)
    writer.finalize(world)

    with pytest.raises(ValueError, match="duplicate seed"):
        compare_run_sets([tmp_path / "run", tmp_path / "run"], [tmp_path / "run"])
    other_scenario = ScenarioSpec.model_validate(
        json.loads(FIXTURE.read_text(encoding="utf-8"))
        | {"duration_minutes": 3, "seed": scenario.seed + 1}
    )
    other_world = WorldState.from_scenario(other_scenario)
    other_writer = RunArtifactWriter(other_scenario, tmp_path / "other-seed")
    other_writer.write_initial(other_world)
    other_writer.capture_metric(other_world, minute=0)
    other_writer.finalize(other_world)
    with pytest.raises(ValueError, match="same seeds"):
        compare_run_sets([tmp_path / "run"], [tmp_path / "other-seed"])


def test_causal_trace_connects_facility_event_queue_wait_agent_and_staff() -> None:
    decisions = [
        {
            "minute_index": 10,
            "agent_id": "A001",
            "action": "request_toilet",
            "target_id": "toilet_1",
        },
        {
            "minute_index": 11,
            "agent_id": "A002",
            "action": "seek_help",
            "target_id": "support-request-2",
        },
    ]
    events = [
        {
            "realized_minute": 5,
            "status": "applied",
            "event": {
                "event_id": "outage-1",
                "event_type": "facility_outage",
                "payload": {"facility_id": "toilet_1", "units_out": 1},
                "provenance": {},
            },
        },
        {
            "realized_minute": 19,
            "status": "applied",
            "event": {
                "event_id": "toilet-complete-4",
                "event_type": "toilet_service_complete",
                "payload": {
                    "facility_id": "toilet_1",
                    "agent_id": "A001",
                    "task_id": "toilet-service-4",
                },
                "source_id": "toilet-service-4",
                "causation_id": "toilet-queue-A001",
                "provenance": {"requested_at": 10, "queue_wait_minutes": 8},
            },
        },
        {
            "realized_minute": 20,
            "status": "applied",
            "event": {
                "event_id": "staff-complete-7",
                "event_type": "staff_support_complete",
                "payload": {
                    "request_id": "support-request-2",
                    "agent_id": "A002",
                    "staff_id": "S01",
                    "task_id": "staff-task-7",
                },
                "source_id": "staff-task-7",
                "causation_id": "support-request-2",
                "provenance": {},
            },
        },
    ]

    trace = build_causal_trace(decisions, events, focus_agent_id="A001")
    nodes = {node["id"]: node for node in trace["nodes"]}
    edges = {(edge["source"], edge["target"], edge["relationship"]) for edge in trace["edges"]}

    assert nodes["toilet-request:toilet_1:A001:10"]["details"]["queue_wait_minutes"] == 8
    assert (
        "event:outage-1",
        "toilet-request:toilet_1:A001:10",
        "capacity_context",
    ) in edges
    assert ("agent:A001", "decision:10:A001:0", "made_by") in edges
