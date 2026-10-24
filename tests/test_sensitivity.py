"""Paired sensitivity summaries and repeatable small-run outputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.comparison import compare_sensitivity_run_sets
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

ROOT = Path(__file__).parents[1]


def _record(
    *,
    seed: int,
    value: float,
    metric: float,
    condition: str,
    input_hash: str = "a" * 64,
) -> dict[str, object]:
    return {
        "case_id": "sensitivity-test",
        "scenario_id": "sensitivity-test-scenario",
        "seed": seed,
        "condition": condition,
        "parameter_value": value,
        "input_hash": input_hash,
        "scenario_hash": hashlib.sha256(f"scenario-{seed}-{condition}".encode()).hexdigest(),
        "model_config_hash": hashlib.sha256(f"model-{condition}".encode()).hexdigest(),
        "final_state_hash": hashlib.sha256(f"state-{seed}-{condition}".encode()).hexdigest(),
        "metrics": {"stress_mean": metric},
    }


def _compare(
    baseline: list[dict[str, object]], variant: list[dict[str, object]]
) -> dict[str, object]:
    return compare_sensitivity_run_sets(
        baseline,
        variant,
        case_id="sensitivity-test",
        parameter_path="model_configs.psychology.stress_pressure",
        baseline_value=0.0,
        variant_value=0.1,
        assumption_id="ASM-H04-CAUSAL-STRESS-V1",
        model_version="causal-stress-v1",
        expected_seeds=(3, 4, 5),
    )


def test_paired_sensitivity_report_summarizes_seed_spread_and_provenance() -> None:
    baseline = [
        _record(seed=seed, value=0.0, metric=seed, condition="baseline") for seed in (3, 4, 5)
    ]
    variant = [
        _record(seed=seed, value=0.1, metric=seed + offset, condition="variant")
        for seed, offset in zip((3, 4, 5), (1.0, 2.0, 3.0), strict=True)
    ]

    report = _compare(baseline, variant)

    metric = report["metrics"]["stress_mean"]
    assert report["seeds"] == [3, 4, 5]
    assert metric["paired_delta"]["mean"] == pytest.approx(2.0)
    assert metric["paired_delta"]["stdev"] == pytest.approx(1.0)
    assert metric["paired_delta"]["positive_seed_count"] == 3
    assert metric["paired_delta"]["same_nonzero_direction"] is True
    assert report["parameter"]["source_type"] == "assumption"
    assert report["parameter"]["assumption_id"] == "ASM-H04-CAUSAL-STRESS-V1"
    assert "no confidence interval" in " ".join(report["uncertainty_notes"]).casefold()


def test_paired_sensitivity_rejects_unpaired_inputs_and_wrong_parameter_values() -> None:
    baseline = [
        _record(seed=seed, value=0.0, metric=0.2, condition="baseline") for seed in (3, 4, 5)
    ]
    variant = [_record(seed=seed, value=0.1, metric=0.3, condition="variant") for seed in (3, 4, 5)]

    with pytest.raises(ValueError, match="same paired seeds"):
        _compare(baseline, variant[:2])

    wrong_input = [dict(item) for item in variant]
    wrong_input[0] = {**wrong_input[0], "input_hash": "b" * 64}
    with pytest.raises(ValueError, match="identical inputs"):
        _compare(baseline, wrong_input)

    wrong_value = [dict(item) for item in variant]
    wrong_value[0] = {**wrong_value[0], "parameter_value": 0.2}
    with pytest.raises(ValueError, match="declared parameter value"):
        _compare(baseline, wrong_value)


def test_paired_sensitivity_rejects_duplicate_seeds() -> None:
    baseline = [
        _record(seed=seed, value=0.0, metric=0.2, condition="baseline") for seed in (3, 4, 5)
    ]
    variant = [_record(seed=seed, value=0.1, metric=0.3, condition="variant") for seed in (3, 4, 5)]
    baseline.append(baseline[0])

    with pytest.raises(ValueError, match="duplicate seed"):
        _compare(baseline, variant)


def test_same_seed_small_scenario_has_identical_canonical_outputs(tmp_path: Path) -> None:
    scenario = ScenarioSpec.load(ROOT / "fixtures/scenarios/h00_small_replay.json")
    first = run_scenario(scenario, tmp_path / "first", trace_level=TraceLevel.OFF)
    second = run_scenario(scenario, tmp_path / "second", trace_level=TraceLevel.SUMMARY)

    assert first.canonical_json() == second.canonical_json()
    for filename in ("metrics.csv", "events_realized.jsonl"):
        assert (tmp_path / "first" / filename).read_bytes() == (
            tmp_path / "second" / filename
        ).read_bytes()
    manifests = [
        json.loads((tmp_path / name / "manifest.json").read_text(encoding="utf-8"))
        for name in ("first", "second")
    ]
    assert manifests[0]["final_state_hash"] == manifests[1]["final_state_hash"]
    assert manifests[0]["scenario_hash"] == manifests[1]["scenario_hash"]
