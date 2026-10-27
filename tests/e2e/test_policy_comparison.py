"""Two Runs that differ only in the registered behavior policy can be compared, and the
CLI reports the variants; any other change is refused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.__main__ import main
from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.comparison import compare_run_artifacts
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec


def test_policy_comparison_requires_only_the_registered_variant_to_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_fixture = Path(__file__).parents[2] / "fixtures/scenarios/h00_small_replay.json"
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
