"""Immutable offline matrix/calibration plans and saved-result management.

No simulation is launched by plan/collect/fit. Cell Run preparation uses ProjectAPI;
normal explicit resume is the only path that subsequently computes a cell.
"""

from __future__ import annotations

import json
import math
import statistics
from typing import Any, Literal

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import ExtensionModel, Finite, Identifier
from sheltersim.experiments.validation import EvaluationPlan, _variant_scenario
from sheltersim.storage.evaluations import EvaluationStore
from sheltersim.storage.projects import ProjectStore, StorageError, canonical, digest, safe_document

METRIC_UNITS = {
    "stress_mean": "normalized_0_1",
    "toilet_wait_mean_minutes": "minute",
    "toilet_wait_total_minutes": "minute",
    "toilet_wait_max_minutes": "minute",
    "toilet_queue_length": "count",
    "toilet_active_users": "count",
    "toilet_working_units": "count",
    "stress_high_count": "count",
    "staff_busy_count": "count",
    "staff_assigned_task_count": "count",
    "unresolved_support_request_count": "count",
    "staff_load_ratio": "normalized_0_1",
    "support_no_qualified_worker_count": "count",
    "support_no_qualified_worker_request_minutes": "request_minute",
    "support_fairness_promoted_assignment_count": "count",
    "support_wait_staff_busy_request_minutes": "request_minute",
    "support_wait_staff_off_shift_request_minutes": "request_minute",
    "water_ml_remaining": "mL",
    "food_meal_units_remaining": "meal_unit",
}


class ObservationTarget(ExtensionModel):
    dataset_id: Identifier
    metric: Identifier
    value: Finite
    scale: Finite = Field(gt=0)
    weight: Finite = Field(default=1, gt=0)
    role: Literal["training", "holdout"] = "training"


class ExperimentInput(ExtensionModel):
    schema_version: Literal[1] = 1
    scenario_id: Identifier
    scenario_version: int = Field(strict=True, ge=1)
    purpose: Literal["model_comparison", "sensitivity", "calibration"] = "model_comparison"
    plan: EvaluationPlan
    observations: tuple[ObservationTarget, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def bounded_matrix(self) -> ExperimentInput:
        if len(self.plan.seeds) * len(self.plan.variants) > 256:
            raise ValueError("matrix exceeds 256 cells")
        if any(metric not in METRIC_UNITS for metric in self.plan.metrics):
            raise ValueError("experiment metric unit is not registered")
        if self.purpose == "calibration" and not any(
            row.role == "training" for row in self.observations
        ):
            raise ValueError("calibration requires explicit training observations")
        return self


def _score(rows: list[dict[str, Any]], targets: list[ObservationTarget]) -> list[float]:
    if not targets:
        return []
    try:
        result = [
            sum(
                obs.weight * ((row["metrics"][obs.metric] - obs.value) / obs.scale) ** 2
                for obs in targets
            )
            / sum(obs.weight for obs in targets)
            for row in rows
        ]
    except (OverflowError, ZeroDivisionError) as exc:
        raise StorageError("calibration_score_nonfinite") from exc
    if any(not math.isfinite(value) for value in result):
        raise StorageError("calibration_score_nonfinite")
    return result


class ExperimentStore:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def _path(self, project: str, experiment: str, *parts: str) -> Any:
        return self.store._path(project, "experiments", experiment, *parts)

    def plan(self, project: str, experiment: str) -> dict[str, Any]:
        record = self.store._read(self._path(project, experiment, "experiment.json"))
        if digest({k: v for k, v in record.items() if k != "plan_hash"}) != record["plan_hash"]:
            raise StorageError("experiment_plan_integrity_mismatch")
        return record

    def create(self, project: str, experiment: str, document: dict[str, Any]) -> dict[str, Any]:
        request = ExperimentInput.model_validate(document)
        base = self.store.scenario(project, request.scenario_id, request.scenario_version)
        base_doc = json.loads(base.canonical_json())
        initial = {k: v for k, v in base_doc.items() if k not in {"seed", "model_configs"}}
        cells = []
        for variant in request.plan.variants:
            for seed in request.plan.seeds:
                scenario = _variant_scenario(base, variant, seed)
                key = f"cell-{len(cells):04d}"
                cells.append(
                    {
                        "cell_id": key,
                        "variant_id": variant.id,
                        "seed": seed,
                        "scenario_hash": digest(json.loads(scenario.canonical_json())),
                        "run_id": "exp-" + digest({"experiment": experiment, "cell": key})[:40],
                    }
                )
        metadata = {
            "scenario_id": base.id,
            "agent_count": len(base.profiles),
            "duration_minutes": base.duration_minutes,
            **base.provenance.get("region_context", {}),
        }
        sources = []
        for observation in request.observations:
            if observation.metric not in request.plan.metrics:
                raise StorageError("observation_metric_not_in_plan")
            dataset = self.store._read(
                self.store._path(project, "datasets", f"{observation.dataset_id}.json")
            )
            if dataset["unit"] != METRIC_UNITS[observation.metric]:
                raise StorageError("dataset_metric_unit_mismatch")
            if not dataset["lower"] <= observation.value <= dataset["upper"]:
                raise StorageError("observation_outside_registered_dataset_range")
            mismatches = {
                key: {"expected": value, "scenario": metadata.get(key)}
                for key, value in dataset["applicability"].items()
                if metadata.get(key) != value
            }
            sources.append(
                {
                    "dataset_id": observation.dataset_id,
                    "dataset_hash": digest(dataset),
                    "source_id": dataset["source_id"],
                    "unit": dataset["unit"],
                    "applicable": not mismatches,
                    "mismatches": mismatches,
                    "external_validation": False,
                }
            )
        body = {
            "schema_version": 1,
            "experiment_id": experiment,
            **request.model_dump(mode="json"),
            "cells": cells,
            "input_hash": digest(initial),
            "dataset_checks": sources,
            "execution_mode": "offline",
            "status": "configured_not_executed",
            "external_validation": False,
            "limits": (
                "Operator-provided aggregate observations. A fitting score does not establish "
                "empirical model validity."
            ),
        }
        if len(canonical(body).encode()) > 1024 * 1024 or safe_document(body) != body:
            raise StorageError("invalid_or_oversized_experiment")
        record = {**body, "plan_hash": digest(body)}
        with self.store.transaction():
            self.store._write(
                self._path(project, experiment, "experiment.json"), record, immutable=True
            )
        return record

    def cell(self, record: dict[str, Any], cell_id: str) -> dict[str, Any]:
        cell = next((row for row in record["cells"] if row["cell_id"] == cell_id), None)
        if cell is None:
            raise StorageError("unknown_experiment_cell")
        return cell

    def prepare(
        self, api: Any, project: str, experiment: str, cell_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        from sheltersim.web.projects import fields

        fields(body, {"approved", "approved_plan_hash"})
        record = self.plan(project, experiment)
        if body["approved"] is not True or body["approved_plan_hash"] != record["plan_hash"]:
            raise StorageError("experiment_cell_requires_plan_approval")
        cell = self.cell(record, cell_id)
        binding = self._path(project, experiment, cell_id + ".json")
        if binding.exists():
            saved = self.store._read(binding)
            if saved["plan_hash"] != record["plan_hash"]:
                raise StorageError("experiment_binding_mismatch")
            run = api.post(
                [project, "runs"],
                {
                    "run_id": saved["run_id"],
                    "scenario_id": record["scenario_id"],
                    "scenario_version": saved["scenario_version"],
                    "execution_mode": "offline",
                },
            )
            return {**saved, "run": run, "status": "prepared_not_executed"}
        base = self.store.scenario(project, record["scenario_id"], record["scenario_version"])
        plan = EvaluationPlan.model_validate(record["plan"])
        variant = next(v for v in plan.variants if v.id == cell["variant_id"])
        scenario = _variant_scenario(base, variant, cell["seed"])
        if digest(json.loads(scenario.canonical_json())) != cell["scenario_hash"]:
            raise StorageError("experiment_model_configuration_changed")
        # Approval covers exact derived inputs. No execution or provider call here.
        saved = self.store.save_scenario(project, scenario, approved_hash=cell["scenario_hash"])
        result = {
            "schema_version": 1,
            **cell,
            "scenario_version": saved["scenario_version"],
            "plan_hash": record["plan_hash"],
        }
        # Durable intent precedes Run initialization, making restart retries reuse
        # the exact immutable Scenario revision rather than allocate another one.
        with self.store.transaction():
            self.store._write(binding, result, immutable=True)
        run = api.post(
            [project, "runs"],
            {
                "run_id": cell["run_id"],
                "scenario_id": scenario.id,
                "scenario_version": saved["scenario_version"],
                "execution_mode": "offline",
            },
        )
        return {**result, "run": run, "status": "prepared_not_executed"}

    def collect(self, project: str, experiment: str, cell_id: str) -> dict[str, Any]:
        from sheltersim.storage.history_index import HistoryIndex

        record = self.plan(project, experiment)
        cell = self.cell(record, cell_id)
        bound = self.store._read(self._path(project, experiment, cell_id + ".json"))
        reference = self.store.branch_reference(project, bound["run_id"], "main")
        scenario = self.store.scenario(project, record["scenario_id"], bound["scenario_version"])
        if reference.computed_until != scenario.duration_minutes:
            raise StorageError("experiment_cell_not_completed")
        index = HistoryIndex(self.store, project, bound["run_id"])
        with index.connect() as db:
            index.ready(db, "main", reference.checkpoint_hash)
            row = db.execute(
                "SELECT payload,payload_hash FROM records WHERE branch='main' AND "
                "kind='kpis' ORDER BY minute DESC,key DESC LIMIT 1"
            ).fetchone()
        if row is None or digest(json.loads(row["payload"])) != row["payload_hash"]:
            raise StorageError("experiment_metric_evidence_missing_or_corrupt")
        metrics = json.loads(row["payload"])
        if any(type(metrics.get(key)) not in (int, float) for key in record["plan"]["metrics"]):
            raise StorageError("experiment_metric_missing")
        if metrics["minute_index"] != scenario.duration_minutes - 1:
            raise StorageError("completed_run_metric_horizon_incomplete")
        header = self.store.checkpoint_fields(
            project, bound["run_id"], reference.checkpoint_hash, ("model_versions", "state_hash")
        )
        result = {
            "schema_version": 1,
            "cell_id": cell_id,
            "variant_id": cell["variant_id"],
            "seed": cell["seed"],
            "input_hash": record["input_hash"],
            "scenario_hash": cell["scenario_hash"],
            "final_state_hash": header["state_hash"],
            "model_versions": header["model_versions"],
            "model_configs": scenario.model_configs,
            "metrics": {key: metrics[key] for key in record["plan"]["metrics"]},
            "dataset_checks": [],
            "evidence": {
                "run_id": bound["run_id"],
                "branch_id": "main",
                "checkpoint_hash": reference.checkpoint_hash,
                "metric_minute": metrics["minute_index"],
                "metric_payload_hash": row["payload_hash"],
                "time_semantics": "end_of_tick",
            },
            "status": "recorded_not_verified",
            "external_validation": False,
        }
        with self.store.transaction():
            path = self._path(project, experiment, "results", cell_id + ".json")
            existed = path.exists()
            self.store._write(path, result, immutable=True)
            if not existed:
                pointer = self._path(project, experiment, "result-generation.json")
                previous = self.store._read(pointer)["generation"] if pointer.exists() else 0
                self.store._write(pointer, {"schema_version": 1, "generation": previous + 1})
        return result

    def finalize(self, project: str, experiment: str, evaluation_id: str) -> dict[str, Any]:
        record = self.plan(project, experiment)
        rows = []
        for cell in record["cells"]:
            result = self.store._read(
                self._path(project, experiment, "results", cell["cell_id"] + ".json")
            )
            rows.append(
                {
                    key: result[key]
                    for key in (
                        "variant_id",
                        "seed",
                        "input_hash",
                        "scenario_hash",
                        "model_versions",
                        "model_configs",
                        "metrics",
                        "dataset_checks",
                    )
                }
            )
        report = {"plan": record["plan"], "runs": rows}
        return EvaluationStore(self.store).import_report(
            project, evaluation_id, report, digest(report)
        )

    def fit(self, project: str, experiment: str) -> dict[str, Any]:
        record = self.plan(project, experiment)
        if record["purpose"] != "calibration":
            raise StorageError("experiment_is_not_a_calibration_plan")
        observations = [ObservationTarget.model_validate(row) for row in record["observations"]]
        applicable = {row["dataset_id"] for row in record["dataset_checks"] if row["applicable"]}
        if not any(row.role == "training" and row.dataset_id in applicable for row in observations):
            raise StorageError("no_applicable_training_observation")
        scores = []
        for variant in record["plan"]["variants"]:
            rows = [
                self.store._read(
                    self._path(project, experiment, "results", cell["cell_id"] + ".json")
                )
                for cell in record["cells"]
                if cell["variant_id"] == variant["id"]
            ]
            summary = {
                "variant_id": variant["id"],
                "parameters": variant["parameters"],
                "policies": variant["policies"],
            }
            for role in ("training", "holdout"):
                targets = [
                    obs for obs in observations if obs.role == role and obs.dataset_id in applicable
                ]
                per_seed = _score(rows, targets)
                if any(not math.isfinite(value) for value in per_seed):
                    raise StorageError("calibration_score_nonfinite")
                summary[role + "_score"] = statistics.fmean(per_seed) if per_seed else None
                summary[role + "_seed_scores"] = per_seed
            scores.append(summary)
        candidate = min(scores, key=lambda row: row["training_score"])
        result = {
            "schema_version": 1,
            "experiment_id": experiment,
            "plan_hash": record["plan_hash"],
            "scores": scores,
            "candidate": candidate,
            "selection_basis": (
                "training_weighted_scaled_squared_error; holdout did not select candidate"
            ),
            "status": "candidate_parameters_not_applied",
            "external_validation": False,
            "verified": False,
            "limits": (
                "Simulation/observation fit only. Scales, source applicability and structural "
                "uncertainty require domain review; no model certification or original Run "
                "mutation."
            ),
        }
        with self.store.transaction():
            self.store._write(self._path(project, experiment, "fit.json"), result, immutable=True)
        return result
