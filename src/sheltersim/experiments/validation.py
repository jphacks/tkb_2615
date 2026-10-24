"""Scientific provenance, applicability and paired descriptive evaluation."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import ExtensionModel, Identifier
from sheltersim.contracts.policies import model_catalog, model_signatures, selected_models
from sheltersim.core.engine import TraceLevel
from sheltersim.domain.models import InformationSpreadConfig
from sheltersim.domain.staff import default_support_fairness_minutes
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import (
    RESOURCE_SERVICE_POLICY_ID,
    PsychologyModelConfig,
    ResourceServiceConfig,
    ScenarioSpec,
    ToiletServiceConfig,
)
from sheltersim.policies.social import SocialPropagationParameters
from sheltersim.storage.projects import canonical, digest, safe_document

LEGEND = {
    "A": "対象・単位・期間が一致する実測資料。適用条件の確認が必要です。",
    "B": "論文・行政資料に基づく考え方や運営上の条件。今回の係数を実測した根拠とは限りません。",
    "C": "合成条件・モデル仮定。現実の心理・健康・安全性の予測として検証されていません。",
}


class EvaluationVariant(ExtensionModel):
    id: Identifier
    policies: dict[str, str] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_parameter_values(self) -> EvaluationVariant:
        if len(self.parameters) > 64:
            raise ValueError("evaluation variants may override at most 64 parameters")
        shift_path = "staff_dynamics.shift_windows_by_staff.*"
        for path, value in self.parameters.items():
            if path == shift_path:
                from sheltersim.domain.staff_dynamics import validate_staff_shift_windows_override

                validate_staff_shift_windows_override(value)
                continue
            if isinstance(value, dict):
                if not value or len(value) > 100:
                    raise ValueError("structured evaluation overrides require 1..100 entity values")
                for entry in value.values():
                    fields = entry.values() if isinstance(entry, dict) else (entry,)
                    if isinstance(entry, dict) and (not entry or len(entry) > 4):
                        raise ValueError("per-zone evaluation overrides allow 1..4 typed fields")
                    if any(
                        type(number) not in (int, float) or not math.isfinite(number)
                        for number in fields
                    ):
                        raise ValueError("evaluation override values must be finite numbers")
            elif type(value) in (int, float):
                if not math.isfinite(value):
                    raise ValueError("evaluation override values must be finite numbers")
            elif type(value) is not bool:
                raise ValueError("evaluation overrides must be scalars or bounded numeric maps")
        return self


class EvaluationPlan(ExtensionModel):
    schema_version: Literal[1] = 1
    seeds: tuple[int, ...] = Field(min_length=1, max_length=100)
    variants: tuple[EvaluationVariant, ...] = Field(min_length=2, max_length=30)
    metrics: tuple[str, ...] = ("stress_mean", "toilet_wait_mean_minutes", "unresolved_support_request_count")

    @model_validator(mode="after")
    def paired_seeds_and_variants(self) -> EvaluationPlan:
        if len(set(self.seeds)) != len(self.seeds) or any(seed < 0 for seed in self.seeds):
            raise ValueError("evaluation seeds must be unique and nonnegative")
        if len({v.id for v in self.variants}) != len(self.variants):
            raise ValueError("evaluation variant IDs must be unique")
        return self


def parameter_catalog(scenario: ScenarioSpec) -> list[dict[str, Any]]:
    """Effective values plus declared units/ranges; all current coefficients are C."""
    raw_staff = scenario.model_configs.get("staff", {})
    staff_config = {
        "support_task_minutes": 10,
        "unresolved_after_minutes": 90,
        "support_cooldown_minutes": 30,
        **raw_staff,
    }
    if "support_fairness_minutes" not in raw_staff:
        timeout_minutes = staff_config["unresolved_after_minutes"]
        staff_config["support_fairness_minutes"] = default_support_fairness_minutes(timeout_minutes)
    models = selected_models(scenario.model_configs)
    catalog = model_catalog()
    configs: dict[str, Any] = {
        "psychology": PsychologyModelConfig.model_validate(scenario.model_configs.get("psychology", {})).coefficient_values(),
        "social_propagation": asdict(SocialPropagationParameters.from_mapping(
            scenario.model_configs.get("social_propagation"),
            source_id=catalog[models["social"]]["assumption_source_id"],
        )),
        "information_spread": InformationSpreadConfig.from_mapping(scenario.model_configs.get("information_spread")).to_dict(),
        "behavior": {"temperature": 0.2, "decision_interval_minutes": 15, **scenario.model_configs.get("behavior", {})},
        "toilet_service": ToiletServiceConfig.model_validate(
            scenario.model_configs.get("toilet_service", {})
        ).model_dump(mode="json"),
        "staff": staff_config,
    }
    if "resource_service" in scenario.model_configs:
        configs["resource_service"] = ResourceServiceConfig.model_validate(scenario.model_configs["resource_service"]).model_dump(mode="json")
    sources = {"psychology": "ASM-H04-CAUSAL-STRESS-V1", "social_propagation": "ASM-SOCIAL-PROPAGATION-V1", "information_spread": "ASM-INFORMATION-SPREAD-V1", "behavior": "ASM-UTILITY-BEHAVIOR-V1", "toilet_service": "ASM-TOILET-SERVICE-DURATION-V1", "staff": "ASM-STAFF-SUPPORT-LIFECYCLE-V1", "resource_service": "ASM-H02-RESOURCE-SERVICE-V1"}
    for domain, name in (("psychology", "psychology"), ("behavior", "behavior"), ("social", "social_propagation"), ("information", "information_spread")):
        sources[name] = catalog[models[domain]]["assumption_source_id"]
    result = []
    for domain, config in configs.items():
        for name, value in config.items():
            if type(value) not in (float, int, bool) or name == "schema_version":
                continue
            unit = "boolean" if type(value) is bool else "minute" if "minutes" in name else "mL" if "_ml" in name else "meal_unit" if "meal_units" in name else "count" if "contacts" in name else "normalized_or_multiplier"
            lower, upper = 0.0, None
            if domain == "staff" and name == "support_fairness_minutes":
                lower = 1
                upper = config.get("unresolved_after_minutes")
            if domain == "psychology":
                upper = 1.0 if name == "need_pressure_threshold" else 0.25
                unit = "normalized_0_1" if name == "need_pressure_threshold" else "normalized_per_minute"
            result.append({"path": f"{domain}.{name}", "value": value, "unit": unit, "lower": lower, "upper": upper, "evidence_grade": "C", "source_id": sources[domain], "applicability": "synthetic single-shelter hypothesis; no empirical calibration", "external_validation": False})
    by_path = {row["path"]: row for row in result}
    from sheltersim.contracts.evidence import operator_evidence_catalog
    from sheltersim.domain.simulator_state import simulator_signatures
    from sheltersim.domain.staff_operations import STAFF_OPERATIONS_POLICY_VERSION
    catalog = operator_evidence_catalog()
    active_model_ids = list(models.values())
    if "simulator:staff_operations_v1" in simulator_signatures(scenario.model_configs):
        active_model_ids.append(STAFF_OPERATIONS_POLICY_VERSION)
    if "simulator:movement_v1" in simulator_signatures(scenario.model_configs):
        from sheltersim.domain.movement import MOVEMENT_POLICY_VERSION
        active_model_ids.append(MOVEMENT_POLICY_VERSION)
    if "resource_service" in scenario.model_configs:
        active_model_ids.append(RESOURCE_SERVICE_POLICY_ID)
    if "toilet_service" in scenario.model_configs:
        from sheltersim.domain.facilities import TOILET_SERVICE_POLICY_VERSION

        active_model_ids.append(TOILET_SERVICE_POLICY_VERSION)
    from sheltersim.domain.staff import STAFF_SUPPORT_POLICY_VERSION
    from sheltersim.domain.staff_dynamics import (
        SHIFT_STAFF_DYNAMICS_POLICY_VERSION,
        STAFF_DYNAMICS_POLICY_VERSION,
    )

    if "staff" in scenario.model_configs:
        active_model_ids.append(STAFF_SUPPORT_POLICY_VERSION)
    dynamics_policy = scenario.model_configs.get("staff_dynamics", {}).get("policy_version")
    if dynamics_policy in {STAFF_DYNAMICS_POLICY_VERSION, SHIFT_STAFF_DYNAMICS_POLICY_VERSION}:
        active_model_ids.append(dynamics_policy)
    for model_id in active_model_ids:
        for path, metadata in catalog[model_id].get("parameters", {}).items():
            value: Any = scenario.model_configs
            for part in path.split("."):
                if not isinstance(value, dict) or part not in value:
                    value = metadata.get("default")
                    break
                value = value[part]
            if type(value) not in (int, float, bool):
                continue
            source_id = metadata.get("source_id", catalog[model_id]["assumption_source_id"])
            if model_id in {
                RESOURCE_SERVICE_POLICY_ID,
                TOILET_SERVICE_POLICY_VERSION,
                STAFF_SUPPORT_POLICY_VERSION,
                STAFF_DYNAMICS_POLICY_VERSION,
                SHIFT_STAFF_DYNAMICS_POLICY_VERSION,
            }:
                section = {
                    RESOURCE_SERVICE_POLICY_ID: "resource_service",
                    TOILET_SERVICE_POLICY_VERSION: "toilet_service",
                    STAFF_SUPPORT_POLICY_VERSION: "staff",
                    STAFF_DYNAMICS_POLICY_VERSION: "staff_dynamics",
                    SHIFT_STAFF_DYNAMICS_POLICY_VERSION: "staff_dynamics",
                }[model_id]
                source_id = scenario.model_configs[section]["source_id"]
            if path == "staff.support_fairness_minutes":
                row_upper = scenario.model_configs.get("staff", {}).get(
                    "unresolved_after_minutes", 90
                )
            else:
                row_upper = metadata.get("upper")
            row = {"path": path, "value": value, "unit": metadata["unit"],
                   "lower": metadata.get("lower"), "upper": row_upper,
                   "lower_exclusive": metadata.get("lower_exclusive", False),
                   "source_id": source_id,
                   "evidence_grade": metadata.get("evidence_grade", "C"),
                   "applicability": metadata.get("applicability", "installed model assumption"),
                   "external_validation": False}
            by_path[path] = row
    for model_id in active_model_ids:
        for path, metadata in catalog[model_id].get("parameters", {}).items():
            scope = metadata.get("scope")
            parts = path.split(".")
            if scope is None or len(parts) != 3 or parts[2] != "*":
                continue
            section, field = parts[:2]
            config = scenario.model_configs.get(section, {})
            value = (
                config.get(field, metadata.get("default", {}))
                if isinstance(config, dict)
                else metadata.get("default", {})
            )
            by_path[path] = {
                "path": path,
                "value": value,
                "unit": metadata["unit"],
                "lower": metadata.get("lower"),
                "upper": metadata.get("upper"),
                "scope": scope,
                "field_units": metadata.get("field_units"),
                "field_bounds": metadata.get("field_bounds"),
                "source_id": metadata.get("source_id", catalog[model_id]["assumption_source_id"]),
                "evidence_grade": metadata.get("evidence_grade", "C"),
                "applicability": metadata.get("applicability", "installed model assumption"),
                "external_validation": False,
            }
    return [by_path[key] for key in sorted(by_path)]


def evidence_context(scenario: ScenarioSpec, source_manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    if source_manifest is None:
        source_path = Path(__file__).resolve().parents[3] / "fixtures" / "source-manifest.json"
        source_manifest = json.loads(source_path.read_text(encoding="utf-8")) if source_path.is_file() else {}
    models = selected_models(scenario.model_configs)
    catalog = model_catalog()
    dataset = json.loads(files("sheltersim.contracts").joinpath("validation-datasets.json").read_text(encoding="utf-8"))
    research = scenario.provenance.get("region_research", {})
    return {
        "schema_version": 1,
        "legend": LEGEND,
        "models": {domain: {"id": key, **{name: value for name, value in catalog[key].items()
                                         if name != "parameters"}}
                   for domain, key in models.items()},
        "model_versions": model_signatures(scenario.model_configs),
        "parameters": parameter_catalog(scenario),
        "source_manifest_hash": digest(source_manifest) if source_manifest else None,
        "sources_available": bool(source_manifest),
        "sources": safe_document((source_manifest or {}).get("sources", [])),
        "scenario_sources": safe_document(scenario.provenance.get("sources", [])),
        "scenario_assumptions": safe_document(scenario.provenance.get("assumptions", [])),
        "region_limits": safe_document({key: research.get(key, []) for key in ("unknowns", "warnings")}),
        "dataset_registry": dataset,
        "external_validation": False,
        "interpretation": "計算結果は仮定に依存する試算です。単一seedの改善を強い因果効果や実際の安全性の予測として扱いません。",
    }


def paired_summary(baseline: list[dict[str, Any]], candidate: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    def indexed(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        result = {row["seed"]: row for row in rows}
        if len(result) != len(rows):
            raise ValueError("duplicate paired seed")
        return result
    before, after = indexed(baseline), indexed(candidate)
    if set(before) != set(after) or not before:
        raise ValueError("same paired seeds required")
    deltas = []
    for seed in sorted(before):
        if before[seed]["input_hash"] != after[seed]["input_hash"]:
            raise ValueError("paired initial inputs do not match")
        delta = after[seed]["metrics"][metric] - before[seed]["metrics"][metric]
        if not math.isfinite(delta):
            raise ValueError("paired metrics must be finite")
        deltas.append(delta)
    mean = statistics.fmean(deltas)
    stdev = statistics.stdev(deltas) if len(deltas) > 1 else None
    # Approximate 95% Student-t interval; descriptive across chosen synthetic seeds.
    critical = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 15: 2.131, 20: 2.086, 30: 2.042, 60: 2.0}
    interval = None
    if stdev is not None:
        df = len(deltas) - 1
        conservative_df = max((key for key in critical if key <= df), default=1)
        half_width = critical[conservative_df] * stdev / math.sqrt(len(deltas))
        interval = [mean-half_width, mean+half_width]
    return {"metric": metric, "paired_seed_count": len(deltas), "paired_deltas": deltas, "mean_delta": mean, "sample_stdev": stdev, "approximate_95_percent_seed_mean_interval": interval, "uncertain": interval is None or interval[0] <= 0 <= interval[1] or len(deltas) < 3, "interpretation": "Chosen-seed descriptive simulation differences; interval excludes structural model uncertainty and is not a real-world causal confidence interval."}


def check_dataset(dataset: dict[str, Any], scenario: ScenarioSpec, metrics: dict[str, float]) -> dict[str, Any]:
    applicability = dataset.get("applicability", {})
    metadata = {"scenario_id": scenario.id, "agent_count": len(scenario.profiles), "duration_minutes": scenario.duration_minutes, **scenario.provenance.get("region_context", {})}
    if any(metadata.get(key) != value for key, value in applicability.items()):
        return {"dataset_id": dataset["id"], "status": "not_applicable"}
    metric = dataset["metric"]
    if metric not in metrics:
        return {"dataset_id": dataset["id"], "status": "missing_metric"}
    value = metrics[metric]
    return {"dataset_id": dataset["id"], "source_id": dataset["source_id"], "classification": dataset["classification"], "value": value, "within_registered_range": dataset["lower"] <= value <= dataset["upper"], "external_validation": dataset.get("evidence_grade") == "A" and dataset["classification"] == "matched_empirical_observation", "limits": dataset["limits"]}


def _variant_scenario(base: ScenarioSpec, variant: EvaluationVariant, seed: int) -> ScenarioSpec:
    document = json.loads(base.canonical_json())
    document["seed"] = seed
    configs = document["model_configs"]
    if variant.policies:
        selectors = configs.setdefault("policy_registry", {"schema_version": 1})
        if "suite_id" in selectors:
            # A sensitivity variant changes one or more selectors explicitly,
            # so it becomes a schema-2 custom set rather than a suite member.
            selectors.pop("suite_id")
            selectors["schema_version"] = 2
        selectors.update(variant.policies)
        if "behavior" in variant.policies:
            if variant.policies["behavior"] not in model_catalog() or model_catalog()[variant.policies["behavior"]]["domain"]!="behavior":
                raise ValueError("uninstalled behavior model in evaluation variant")
            configs.setdefault("behavior", {})["policy"] = variant.policies["behavior"]
            configs["behavior"]["source_id"] = model_catalog()[variant.policies["behavior"]]["assumption_source_id"]
    if variant.policies.get("psychology") in {"causal-stress-v1","bounded-stress-v1"}:
        configs.pop("body_dynamics",None)
        registry = configs.get("policy_registry", {})
        for dependent_domain in ("body_dynamics", "care_outcome", "affect"):
            if dependent_domain not in variant.policies:
                registry.pop(dependent_domain, None)
    if "affect" in variant.policies:
        configs.setdefault("body_dynamics", {})["affect_policy"] = variant.policies["affect"]
    selected_base=ScenarioSpec.model_validate(document)
    valid_parameters = {entry["path"]: entry for entry in parameter_catalog(selected_base)}
    structured_overrides: dict[str, Any] = {}

    def ensure_assumption_source(source_id: str) -> None:
        sources = document["provenance"].setdefault("sources", [])
        if not any(source.get("id") == source_id for source in sources):
            sources.append({
                "id": source_id,
                "source_kind": "assumption",
                "title": f"Installed model coefficient {source_id}",
                "description": "Experimental model coefficient metadata; no empirical calibration claim.",
            })

    for path, value in variant.parameters.items():
        if path not in valid_parameters:
            raise ValueError(f"unregistered sensitivity parameter: {path}")
        declaration = valid_parameters[path]
        if declaration.get("scope") is not None:
            if not isinstance(value, dict):
                raise ValueError("structured sensitivity parameter requires an entity map")
            structured_overrides[path] = value
            continue
        if declaration["unit"] == "boolean":
            if type(value) is not bool:
                raise ValueError("boolean sensitivity parameter requires a boolean")
        else:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("sensitivity parameter requires a finite number")
            lower, upper = declaration.get("lower"), declaration.get("upper")
            if ((lower is not None and (value < lower or value == lower and declaration.get("lower_exclusive")))
                    or (upper is not None and value > upper)):
                raise ValueError("sensitivity parameter is outside its registered range")
        domain, name = path.split(".", 1)
        if domain == "psychology":
            configs.setdefault(domain, PsychologyModelConfig().to_model_config())
        target = configs.setdefault(domain, {})
        parts = name.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
            if not isinstance(target, dict):
                raise ValueError("sensitivity path traverses a non-object value")
        target[parts[-1]] = value
        ensure_assumption_source(declaration["source_id"])
        document["provenance"].setdefault("assumptions", []).append({
            "id": "ASM-EVALUATION-OVERRIDE-" + hashlib.sha256(
                f"{variant.id}:{path}".encode("utf-8")
            ).hexdigest()[:12].upper(),
            "path": path,
            "subject": path,
            "value": value,
            "unit": declaration["unit"],
            "evidence_grade": declaration.get("evidence_grade", "C"),
            "applicability": declaration.get("applicability", "installed model assumption"),
            "source_id": declaration["source_id"],
            "reason": (
                f"Evaluation Variant {variant.id} selected this value from the declared "
                "Model Catalog range; it is not empirically calibrated."
            ),
        })
    if structured_overrides:
        from sheltersim.experiments.initial_scenario import (
            _apply_selected_policy_parameter_overrides,
        )

        applied = _apply_selected_policy_parameter_overrides(
            configs,
            structured_overrides,
            entity_ids={
                "per_agent_id": {profile.id for profile in selected_base.profiles},
                "per_staff_id": {staff.id for staff in selected_base.staff},
                "per_zone_id": {zone.id for zone in selected_base.zones},
            },
        )
        document["provenance"].setdefault("assumptions", []).extend(applied)
        for record in applied:
            ensure_assumption_source(record["source_id"])
    return ScenarioSpec.model_validate(document)


def evaluate_models(base: ScenarioSpec, plan: EvaluationPlan, destination: Path, *, source_manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute the requested offline matrix; call only at final QA or user analysis."""
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("evaluation output must be empty; prior results are immutable")
    destination.mkdir(parents=True, exist_ok=True)
    initial = json.loads(base.canonical_json())
    initial.pop("seed")
    # Variants may change only registered model settings in this function.
    initial.pop("model_configs")
    input_hash = digest(initial)
    rows = []
    for variant in plan.variants:
        for seed in plan.seeds:
            scenario = _variant_scenario(base, variant, seed)
            path = destination / variant.id / f"seed-{seed}"
            world = run_scenario(scenario, path, trace_level=TraceLevel.OFF, llm_router=None)
            with (path / "metrics.csv").open(newline="", encoding="utf-8") as stream:
                last = None
                for row in csv.DictReader(stream):
                    last = row
            if last is None or any(metric not in last for metric in plan.metrics):
                raise ValueError("requested evaluation metric is missing")
            metrics = {metric: float(last[metric]) for metric in plan.metrics}
            context = evidence_context(scenario, source_manifest)
            rows.append({"variant_id": variant.id, "seed": seed, "input_hash": input_hash, "scenario_hash": digest(json.loads(scenario.canonical_json())), "model_versions": model_signatures(scenario.model_configs), "model_configs": scenario.model_configs, "metrics": metrics, "final_state_hash": digest(world.to_dict()), "dataset_checks": [check_dataset(data, scenario, metrics) for data in context["dataset_registry"]["datasets"]]})
    baseline_id = plan.variants[0].id
    baseline = [r for r in rows if r["variant_id"] == baseline_id]
    comparisons = [{"baseline_id": baseline_id, "candidate_id": variant.id, "metrics": [paired_summary(baseline, [r for r in rows if r["variant_id"] == variant.id], metric) for metric in plan.metrics]} for variant in plan.variants[1:]]
    report = {"schema_version": 1, "status": "executed", "plan": plan.model_dump(mode="json"), "runs": rows, "comparisons": comparisons, "evidence": evidence_context(base, source_manifest), "limits": "No external validity established. Model structural uncertainty is not captured by seed variation."}
    (destination / "evaluation.json").write_text(canonical(safe_document(report)) + "\n", encoding="utf-8")
    return report
