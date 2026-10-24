#!/usr/bin/env python3
"""Run the bounded A07 paired-seed sensitivity protocol and write its compact report."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sheltersim.core.engine import TraceLevel
from sheltersim.domain.models import InformationSpreadConfig
from sheltersim.experiments.comparison import compare_sensitivity_run_sets
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import PsychologyModelConfig, ScenarioSpec

ROOT = Path(__file__).resolve().parents[1]
SEEDS = (3, 4, 5)
WORKERS = 2
ANALYSIS_ID = "A07-MODEL-SENSITIVITY-2026-10"
ANALYSIS_ASSUMPTION_ID = "ASM-A07-SENSITIVITY-CASES-V1"
RUMOR_ASSUMPTION_ID = "ASM-A07-SENSITIVITY-CASES-V1"
CONTEXT_SOURCE_IDS = ("SRC-CAO-SHELTER-GUIDELINE-2024-12",)


@dataclass(frozen=True, slots=True)
class SensitivityCase:
    case_id: str
    scenario_path: str
    parameter_path: str
    assumption_id: str
    model_version: str
    baseline_value: float | bool
    variant_value: float | bool
    context_source_ids: tuple[str, ...] = CONTEXT_SOURCE_IDS
    add_false_rumor: bool = False


CASES = (
    SensitivityCase(
        case_id="h04-facility-stress-pressure",
        scenario_path="fixtures/scenarios/failure_day3.json",
        parameter_path="model_configs.psychology.stress_pressure",
        assumption_id="ASM-H04-CAUSAL-STRESS-V1",
        model_version="causal-stress-v1",
        baseline_value=0.0,
        variant_value=0.012,
        context_source_ids=(),
    ),
    SensitivityCase(
        case_id="social-propagation-during-water-outage",
        scenario_path="fixtures/scenarios/water_outage.json",
        parameter_path="model_configs.social_propagation.enabled",
        assumption_id="ASM-SOCIAL-PROPAGATION-V1",
        model_version="sparse-family-zone-contact-v1",
        baseline_value=False,
        variant_value=True,
    ),
    SensitivityCase(
        case_id="false-claim-transmission-during-water-outage",
        scenario_path="fixtures/scenarios/water_outage.json",
        parameter_path="model_configs.information_spread.base_transmission_probability",
        assumption_id="ASM-INFORMATION-SPREAD-V1",
        model_version="sparse-information-contact-v1",
        baseline_value=0.0,
        variant_value=0.2,
        add_false_rumor=True,
    ),
)

CORE_METRICS = (
    "stress_mean",
    "stress_high_count",
    "toilet_queue_length",
    "toilet_wait_mean_minutes",
    "toilet_wait_max_minutes",
    "staff_load_ratio",
    "unresolved_support_request_count",
)
OPERATIONAL_METRICS = (
    "hunger_need_mean",
    "hydration_need_mean",
    "water_queue_length",
    "meal_queue_length",
    "water_service_wait_mean_minutes",
    "water_ml_remaining",
    "food_meal_units_remaining",
)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _set_path(document: dict[str, Any], dotted_path: str, value: Any) -> None:
    keys = dotted_path.split(".")
    current = document
    for key in keys[:-1]:
        nested = current.get(key)
        if not isinstance(nested, dict):
            raise ValueError(f"parameter path {dotted_path!r} does not reference an object")
        current = nested
    if keys[-1] not in current:
        raise ValueError(f"parameter path {dotted_path!r} is not present")
    current[keys[-1]] = value


def _build_scenario(
    case: SensitivityCase, seed: int, parameter_value: float | bool
) -> ScenarioSpec:
    path = ROOT / case.scenario_path
    document = json.loads(path.read_text(encoding="utf-8"))
    document["id"] = f"a07-{case.case_id}"
    document["seed"] = seed
    provenance = document["provenance"]
    assumption_ids = set(provenance.get("model_assumption_ids", []))
    assumption_ids.update({case.assumption_id, ANALYSIS_ASSUMPTION_ID})
    provenance["model_assumption_ids"] = sorted(assumption_ids)
    provenance["input_source_ids"] = {
        **provenance.get("input_source_ids", {}),
        "a07_sensitivity_parameter": [case.assumption_id],
        **({"a07_false_claim_schedule": [RUMOR_ASSUMPTION_ID]} if case.add_false_rumor else {}),
    }

    model_configs = document["model_configs"]
    if case.parameter_path == "model_configs.psychology.stress_pressure":
        model_configs["psychology"] = PsychologyModelConfig().model_dump(mode="json")
    if case.parameter_path == "model_configs.information_spread.base_transmission_probability":
        model_configs["information_spread"] = InformationSpreadConfig(
            enabled=True,
            base_transmission_probability=0.2,
        ).to_dict()
    if case.add_false_rumor:
        event_sequence = (
            max((item["sequence"] for item in document["scheduled_events"]), default=0) + 1
        )
        document["scheduled_events"].append(
            {
                "id": "a07-false-claim-day3",
                "kind": "exogenous",
                "due_minute": 2880,
                "priority": 0,
                "sequence": event_sequence,
                "event_type": "information_rumor",
                "payload": {
                    "message_id": "a07-false-claim",
                    "truth_status": "false",
                    "origin_agent_id": "A001",
                },
                "source_id": RUMOR_ASSUMPTION_ID,
            }
        )
    _set_path(document, case.parameter_path, parameter_value)
    scenario = ScenarioSpec.model_validate(document)
    if len(scenario.profiles) != 100 or scenario.duration_minutes != 10_080:
        raise ValueError("A07 sensitivity cases must remain at 100 Agents and seven days")
    return scenario


def _input_hash(scenario: ScenarioSpec, parameter_path: str) -> str:
    document = json.loads(scenario.canonical_json())
    document.pop("seed")
    _set_path(document, parameter_path, "<sensitivity-parameter>")
    return _canonical_hash(document)


def _last_csv_row(path: Path) -> dict[str, str]:
    row = None
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            pass
    if row is None:
        raise ValueError(f"run artifact {path} has no metric rows")
    return row


def _numeric_metrics(run_directory: Path) -> dict[str, float]:
    core = _last_csv_row(run_directory / "metrics.csv")
    metrics: dict[str, float] = {}
    for name in CORE_METRICS:
        if name in core:
            metrics[name] = float(core[name])
    operational_path = run_directory / "operational_kpis.csv"
    if operational_path.exists():
        operational = _last_csv_row(operational_path)
        for name in OPERATIONAL_METRICS:
            if name in operational:
                metrics[name] = float(operational[name])
    information_path = run_directory / "information_spread.json"
    if information_path.exists():
        summary = json.loads(information_path.read_text(encoding="utf-8"))["summary"]
        for name in (
            "agent_message_reach_count",
            "reach_rate",
            "correction_count",
            "correction_rate",
            "support_requests",
        ):
            metrics[f"information_{name}"] = float(summary[name])
        for name, value in summary["counters"].items():
            metrics[f"information_counter_{name}"] = float(value)
    if not metrics:
        raise ValueError(f"run artifact {run_directory} has no supported sensitivity metrics")
    return metrics


def _run_one(task: dict[str, Any]) -> dict[str, Any]:
    case = SensitivityCase(**task["case"])
    scenario = _build_scenario(case, task["seed"], task["parameter_value"])
    run_directory = Path(task["run_directory"])
    run_scenario(scenario, run_directory, trace_level=TraceLevel.OFF)
    manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    return {
        "case_id": case.case_id,
        "scenario_id": scenario.id,
        "seed": scenario.seed,
        "condition": task["condition"],
        "parameter_value": task["parameter_value"],
        "input_hash": _input_hash(scenario, case.parameter_path),
        "scenario_hash": manifest["scenario_hash"],
        "model_config_hash": _canonical_hash(scenario.model_configs),
        "final_state_hash": manifest["final_state_hash"],
        "run_id": manifest["run_id"],
        "metrics": _numeric_metrics(run_directory),
    }


def run_analysis(output_path: Path) -> dict[str, Any]:
    """Run all 18 100-Agent/seven-day inputs and return a deterministic report."""
    fixture_hashes = {case.case_id: _file_hash(ROOT / case.scenario_path) for case in CASES}
    source_manifest_hash = _file_hash(ROOT / "fixtures/source-manifest.json")
    tasks = []
    with TemporaryDirectory(prefix="sheltersim-a07-sensitivity-") as temporary_root:
        run_root = Path(temporary_root)
        for case in CASES:
            for seed in SEEDS:
                for condition, value in (
                    ("baseline", case.baseline_value),
                    ("variant", case.variant_value),
                ):
                    task = {
                        "case": asdict(case),
                        "seed": seed,
                        "condition": condition,
                        "parameter_value": value,
                        "run_directory": str(run_root / f"{case.case_id}-{condition}-{seed}"),
                    }
                    tasks.append(task)
        records = []
        with ProcessPoolExecutor(max_workers=WORKERS) as executor:
            futures = {executor.submit(_run_one, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                record = future.result()
                records.append(record)
                print(
                    f"completed {record['case_id']} {record['condition']} seed={record['seed']}",
                    flush=True,
                )
        analyses = []
        for case in CASES:
            case_records = [item for item in records if item["case_id"] == case.case_id]
            analyses.append(
                compare_sensitivity_run_sets(
                    [item for item in case_records if item["condition"] == "baseline"],
                    [item for item in case_records if item["condition"] == "variant"],
                    case_id=case.case_id,
                    parameter_path=case.parameter_path,
                    baseline_value=case.baseline_value,
                    variant_value=case.variant_value,
                    assumption_id=case.assumption_id,
                    model_version=case.model_version,
                    expected_seeds=SEEDS,
                    context_source_ids=case.context_source_ids,
                )
            )

    report = {
        "schema_version": 1,
        "analysis_id": ANALYSIS_ID,
        "source_type": "assumption",
        "protocol": {
            "agent_count": 100,
            "duration_minutes": 10_080,
            "simulation_seeds": list(SEEDS),
            "paired_conditions_per_case": 2,
            "one_parameter_changed_per_pair": True,
            "profile_generation_seed": 2026,
            "offline_only": True,
            "worker_limit": WORKERS,
        },
        "input_hashes": {
            "source_manifest": source_manifest_hash,
            "scenario_fixtures": fixture_hashes,
        },
        "cases": analyses,
        "interpretation": (
            "Synthetic, conditional simulator outputs. These runs check software sensitivity "
            "and reproducibility; they do not calibrate coefficients or establish "
            "real-world effects."
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs/model-sensitivity-a07.json",
        help="compact report destination (default: docs/model-sensitivity-a07.json)",
    )
    args = parser.parse_args()
    report = run_analysis(args.output)
    print(f"wrote {len(report['cases'])} paired-seed sensitivity cases to {args.output}")


if __name__ == "__main__":
    main()
