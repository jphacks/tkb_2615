"""Immutable analysis imports and source/dataset links, without executing experiments."""

from __future__ import annotations

from typing import Any
from pydantic import Field

from sheltersim.contracts.extensions import ExtensionModel, Finite, Identifier
from sheltersim.contracts.workspace import DatasetInput

from sheltersim.experiments.validation import EvaluationPlan, paired_summary
from sheltersim.storage.projects import ProjectStore, StorageError, canonical, digest, safe_document


class EvaluationRow(ExtensionModel):
    variant_id: Identifier
    seed: int = Field(strict=True, ge=0)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    scenario_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    final_state_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    model_versions: dict[str, str] = Field(min_length=1)
    model_configs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    metrics: dict[str, Finite]
    dataset_checks: tuple[dict[str, Any], ...] = ()


class EvaluationStore:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def import_report(
        self, project_id: str, evaluation_id: str, report: dict[str, Any], approved_hash: str
    ) -> dict[str, Any]:
        if safe_document(report) != report or len(canonical(report).encode()) > 1024 * 1024:
            raise StorageError("invalid_or_oversized_evaluation")
        if digest(report) != approved_hash:
            raise StorageError("evaluation_hash_mismatch")
        if not {"plan", "runs"} <= set(report) or not isinstance(report["runs"], list):
            raise StorageError("invalid_evaluation_shape")
        plan = EvaluationPlan.model_validate(report["plan"])
        runs = [
            EvaluationRow.model_validate(row).model_dump(mode="json", exclude_none=True)
            for row in report["runs"]
        ]
        expected = {(variant.id, seed) for variant in plan.variants for seed in plan.seeds}
        if not isinstance(runs, list) or len(runs) != len(expected):
            raise StorageError("evaluation_matrix_incomplete")
        actual = {(row["variant_id"], row["seed"]) for row in runs}
        if actual != expected:
            raise StorageError("evaluation_matrix_mismatch")
        for row in runs:
            for check in row["dataset_checks"]:
                check["external_validation"] = False
                check["import_status"] = "not_verified"
            if any(type(row["metrics"].get(metric)) not in (int, float) for metric in plan.metrics):
                raise StorageError("evaluation_metric_missing")
            if not isinstance(row.get("model_versions"), dict) or not row.get("input_hash"):
                raise StorageError("evaluation_provenance_missing")
        base_id = plan.variants[0].id
        baseline = [row for row in runs if row["variant_id"] == base_id]
        comparisons = [
            {
                "baseline_id": base_id,
                "candidate_id": variant.id,
                "metrics": [
                    paired_summary(
                        baseline, [r for r in runs if r["variant_id"] == variant.id], metric
                    )
                    for metric in plan.metrics
                ],
            }
            for variant in plan.variants[1:]
        ]
        record = {
            "schema_version": 1,
            "evaluation_id": evaluation_id,
            "input_report_hash": approved_hash,
            "status": "imported_not_verified",
            "external_validation": False,
            "plan": plan.model_dump(mode="json"),
            "runs": runs,
            "comparisons": comparisons,
            "limits": "Imported aggregate results; paired summaries recomputed. No simulation or empirical verification was performed by this import.",
        }
        with self.store.transaction():
            self.store._read(self.store._path(project_id, "project.json"))
            self.store._write(
                self.store._path(project_id, "evaluations", f"{evaluation_id}.json"),
                record,
                immutable=True,
            )
        return record

    def dataset(self, project_id: str, dataset_id: str, document: dict[str, Any]) -> dict[str, Any]:
        document = DatasetInput.model_validate(document).model_dump(mode="json")
        allowed = {
            "source_id",
            "url",
            "title",
            "unit",
            "lower",
            "upper",
            "applicability",
            "model_parameter_links",
        }
        if set(document) != allowed or safe_document(document) != document:
            raise StorageError("invalid_dataset_fields")
        if not isinstance(document["url"], str) or not document["url"].startswith("https://"):
            raise StorageError("dataset_source_requires_https")
        if (
            any(type(document[key]) not in (int, float) for key in ("lower", "upper"))
            or document["lower"] > document["upper"]
        ):
            raise StorageError("invalid_dataset_range")
        if not isinstance(document["model_parameter_links"], list) or not isinstance(
            document["applicability"], dict
        ):
            raise StorageError("invalid_dataset_links")
        from sheltersim.contracts.evidence import operator_evidence_catalog

        catalog = operator_evidence_catalog()
        resolved_links = []
        for link in document["model_parameter_links"]:
            if set(link) != {"model_id", "parameter_path"} or link["model_id"] not in catalog:
                raise StorageError("dataset_model_not_installed")
            parameter = catalog[link["model_id"]].get("parameters", {}).get(link["parameter_path"])
            if parameter is None:
                raise StorageError("dataset_parameter_not_registered")
            resolved_links.append(
                {
                    **link,
                    "parameter_unit": parameter["unit"],
                    "same_unit": parameter["unit"] == document["unit"],
                    "model_source_id": parameter.get(
                        "source_id", catalog[link["model_id"]]["assumption_source_id"]
                    ),
                }
            )
        record = {
            "schema_version": 1,
            "dataset_id": dataset_id,
            **document,
            "resolved_links": resolved_links,
            "status": "operator_supplied_not_verified",
            "external_validation": False,
        }
        with self.store.transaction():
            self.store._read(self.store._path(project_id, "project.json"))
            self.store._write(
                self.store._path(project_id, "datasets", f"{dataset_id}.json"),
                record,
                immutable=True,
            )
        return record
