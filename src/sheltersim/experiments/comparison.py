"""Evidence-oriented causal traces and deterministic A/B comparison reports."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from sheltersim.experiments.branch import BranchRuntime


@dataclass(frozen=True)
class CausalNode:
    """One observed entity or operation in an auditable causal trace."""

    id: str
    kind: str
    minute: int | None
    details: dict[str, Any]


@dataclass(frozen=True)
class CausalEdge:
    """A direct recorded reference or an explicitly labeled contextual link."""

    source: str
    target: str
    relationship: str
    inferred: bool = False


@dataclass(frozen=True)
class BranchComparisonReport:
    """Paired single-seed output with the first observed divergence and evidence."""

    schema_version: int
    scenario_id: str
    seed: int
    baseline_branch_id: str
    candidate_branch_id: str
    first_divergence_minute: int | None
    changed_metrics_at_first_divergence: dict[str, dict[str, Any]]
    final_metric_differences: dict[str, dict[str, Any]]
    changed_agents: list[dict[str, Any]]
    changed_facilities: list[dict[str, Any]]
    changed_staff: list[dict[str, Any]]
    added_event_ids: list[str]
    removed_event_ids: list[str]
    no_op_equivalent: bool
    interpretation: str = (
        "Observed difference for this paired run; a single seed does not establish causal proof."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compare_branches(
    baseline: BranchRuntime,
    candidate: BranchRuntime,
    *,
    maximum_state_differences: int = 200,
) -> BranchComparisonReport:
    """Compare two same-seed branches without turning one run into a causal claim."""
    if baseline.world.scenario_id != candidate.world.scenario_id:
        raise ValueError("branches must use the same scenario")
    if baseline.world.seed != candidate.world.seed:
        raise ValueError("paired branches must use the same seed")
    if maximum_state_differences < 0:
        raise ValueError("maximum_state_differences cannot be negative")

    baseline_metrics = {item.minute_index: asdict(item) for item in baseline.metrics.snapshots}
    candidate_metrics = {item.minute_index: asdict(item) for item in candidate.metrics.snapshots}
    first_minute, first_changes = _first_metric_difference(baseline_metrics, candidate_metrics)
    final_metrics = _metric_differences(
        asdict(baseline.metrics.snapshots[-1]) if baseline.metrics.snapshots else {},
        asdict(candidate.metrics.snapshots[-1]) if candidate.metrics.snapshots else {},
    )
    events_before = {
        record.event.event_id: record.to_dict() for record in baseline.world.event_log.records
    }
    events_after = {
        record.event.event_id: record.to_dict() for record in candidate.world.event_log.records
    }
    changed_agents = _state_differences(
        baseline.world.agents,
        candidate.world.agents,
        lambda item: {
            "zone_id": item.zone_id,
            "activity": item.activity.value,
            "target_id": item.target_id,
            "needs": asdict(item.needs),
            "stress": item.stress,
            "trust": item.trust,
            "next_decision_at": item.next_decision_at,
        },
        maximum_state_differences,
    )
    changed_facilities = _state_differences(
        baseline.world.facilities,
        candidate.world.facilities,
        lambda item: {
            "kind": item.kind,
            "zone_id": item.zone_id,
            "capacity": item.capacity,
            "working_units": item.working_units,
            "active_agent_ids": list(item.active_agent_ids),
            "queue_agent_ids": list(item.queue_agent_ids),
        },
        maximum_state_differences,
    )
    changed_staff = _state_differences(
        baseline.world.staff,
        candidate.world.staff,
        lambda item: {
            "zone_id": item.zone_id,
            "skills": list(item.skills),
            "work_queue": list(item.work_queue),
            "busy_until": item.busy_until,
            "active_task_id": item.active_task_id,
        },
        maximum_state_differences,
    )
    return BranchComparisonReport(
        schema_version=1,
        scenario_id=baseline.world.scenario_id,
        seed=baseline.world.seed,
        baseline_branch_id=baseline.branch_id,
        candidate_branch_id=candidate.branch_id,
        first_divergence_minute=first_minute,
        changed_metrics_at_first_divergence=first_changes,
        final_metric_differences=final_metrics,
        changed_agents=changed_agents,
        changed_facilities=changed_facilities,
        changed_staff=changed_staff,
        added_event_ids=sorted(events_after.keys() - events_before.keys()),
        removed_event_ids=sorted(events_before.keys() - events_after.keys()),
        no_op_equivalent=(baseline.world.canonical_json() == candidate.world.canonical_json()),
    )


def compare_run_artifacts(
    baseline_directory: str | Path,
    candidate_directory: str | Path,
    *,
    allow_behavior_policy_change: bool = False,
) -> dict[str, Any]:
    """Compare saved runs by first KPI divergence and realized-event differences."""
    baseline_path = Path(baseline_directory)
    candidate_path = Path(candidate_directory)
    baseline_manifest = _read_object(baseline_path / "manifest.json")
    candidate_manifest = _read_object(candidate_path / "manifest.json")
    baseline_compatibility = _scenario_compatibility_key(baseline_manifest)
    candidate_compatibility = _scenario_compatibility_key(candidate_manifest)
    behavior_pair = None
    if allow_behavior_policy_change:
        behavior_pair = _validate_behavior_policy_pair(
            baseline_path,
            baseline_manifest,
            candidate_path,
            candidate_manifest,
        )
    elif baseline_compatibility != candidate_compatibility:
        raise ValueError("run artifacts must use the same frozen scenario family")
    if baseline_manifest.get("application_version") != candidate_manifest.get(
        "application_version"
    ):
        raise ValueError("run artifacts must use the same application version")
    baseline_definition = baseline_manifest.get(
        "scenario_definition_hash", baseline_manifest.get("scenario_hash")
    )
    candidate_definition = candidate_manifest.get(
        "scenario_definition_hash", candidate_manifest.get("scenario_hash")
    )
    if baseline_manifest.get("seed") != candidate_manifest.get("seed"):
        raise ValueError("run artifacts must use the same seed for paired comparison")

    before_rows = _run_metric_rows(baseline_path)
    after_rows = _run_metric_rows(candidate_path)
    if (baseline_path / "operational_kpis.csv").exists() != (
        candidate_path / "operational_kpis.csv"
    ).exists():
        raise ValueError("paired runs must both contain operational KPI samples")
    before_by_minute = {row["minute_index"]: row for row in before_rows}
    after_by_minute = {row["minute_index"]: row for row in after_rows}
    first_minute, first_changes = _first_metric_difference(before_by_minute, after_by_minute)
    before_events = _read_jsonl(baseline_path / "events_realized.jsonl")
    after_events = _read_jsonl(candidate_path / "events_realized.jsonl")
    before_ids = {item.get("event", {}).get("event_id") for item in before_events}
    after_ids = {item.get("event", {}).get("event_id") for item in after_events}
    final_before = before_rows[-1] if before_rows else {}
    final_after = after_rows[-1] if after_rows else {}
    report = {
        "schema_version": 1,
        "scenario_hash": baseline_manifest.get("scenario_hash"),
        "scenario_definition_hash": baseline_definition,
        "candidate_scenario_definition_hash": candidate_definition,
        "scenario_family_hash": baseline_compatibility[1],
        "baseline_scenario_id": baseline_manifest.get("scenario_id"),
        "candidate_scenario_id": candidate_manifest.get("scenario_id"),
        "seed": baseline_manifest.get("seed"),
        "baseline_run": str(baseline_path),
        "candidate_run": str(candidate_path),
        "first_divergence_minute": first_minute,
        "changed_metrics_at_first_divergence": first_changes,
        "final_metric_differences": _metric_differences(final_before, final_after),
        "added_event_ids": sorted(item for item in after_ids - before_ids if item is not None),
        "removed_event_ids": sorted(item for item in before_ids - after_ids if item is not None),
        "event_windows": {
            "baseline": _event_windows(baseline_path, before_by_minute),
            "candidate": _event_windows(candidate_path, after_by_minute),
        },
        "interpretation": (
            "Observed paired-run difference for the same frozen profiles, model inputs, and seed; "
            "event schedules may differ. A single seed is descriptive and is not statistical or "
            "causal proof."
        ),
    }
    if behavior_pair is not None:
        report["behavior_policy_comparison"] = behavior_pair
        report["action_counts"] = {
            "baseline": _decision_action_counts(baseline_path),
            "candidate": _decision_action_counts(candidate_path),
        }
        report["need_cohort_comparison"] = _compare_need_cohorts(baseline_path, candidate_path)
        report["interpretation"] = (
            "Observed paired-run difference with identical synthetic profiles, facilities, staff, "
            "events, coefficients, seed, and application version; only the installed behavior "
            "policy changed. A single seed is descriptive and is not evidence of real-world "
            "human behavior or proof that one policy is better."
        )
    return report


def compare_run_sets(
    baseline_directories: Iterable[str | Path],
    candidate_directories: Iterable[str | Path],
    *,
    allow_behavior_policy_change: bool = False,
) -> dict[str, Any]:
    """Pair two multi-seed run sets by seed and include descriptive distributions."""
    baseline_paths = [Path(path) for path in baseline_directories]
    candidate_paths = [Path(path) for path in candidate_directories]
    if not baseline_paths or not candidate_paths:
        raise ValueError("each comparison side needs at least one run")

    def index_by_seed(paths: list[Path]) -> tuple[dict[int, Path], tuple[str, str]]:
        indexed: dict[int, Path] = {}
        scenario_key: tuple[str, str] | None = None
        for path in paths:
            manifest = _read_object(path / "manifest.json")
            seed = manifest.get("seed")
            current_key = _scenario_compatibility_key(manifest)
            if not isinstance(seed, int) or isinstance(seed, bool):
                raise ValueError(f"run {path} has no integer seed in its manifest")
            if not current_key[1]:
                raise ValueError(f"run {path} has no scenario hash in its manifest")
            if seed in indexed:
                raise ValueError(f"run set contains duplicate seed {seed}")
            if scenario_key is not None and current_key != scenario_key:
                raise ValueError("a run set cannot mix scenario families")
            scenario_key = current_key
            indexed[seed] = path
        assert scenario_key is not None
        return indexed, scenario_key

    baseline_by_seed, baseline_hash = index_by_seed(baseline_paths)
    candidate_by_seed, candidate_hash = index_by_seed(candidate_paths)
    behavior_pair = None
    if allow_behavior_policy_change:
        behavior_pair = _validate_behavior_policy_pair(
            baseline_by_seed[min(baseline_by_seed)],
            _read_object(baseline_by_seed[min(baseline_by_seed)] / "manifest.json"),
            candidate_by_seed[min(candidate_by_seed)],
            _read_object(candidate_by_seed[min(candidate_by_seed)] / "manifest.json"),
        )
    elif baseline_hash != candidate_hash:
        raise ValueError("run sets must use the same frozen scenario family")
    if baseline_by_seed.keys() != candidate_by_seed.keys():
        raise ValueError("baseline and candidate run sets must contain the same seeds")
    per_seed = [
        compare_run_artifacts(
            baseline_by_seed[seed],
            candidate_by_seed[seed],
            allow_behavior_policy_change=behavior_pair is not None,
        )
        for seed in sorted(baseline_by_seed)
    ]
    if len(per_seed) == 1:
        return per_seed[0]
    summary = summarize_seed_comparisons(per_seed)
    report = {
        "schema_version": 1,
        "scenario_hash": baseline_hash[1],
        "scenario_hash_kind": baseline_hash[0],
        "seed_count": len(per_seed),
        "seeds": summary["seeds"],
        "per_seed_reports": per_seed,
        "paired_delta_summary": summary["paired_delta_summary"],
        "interpretation": summary["interpretation"],
    }
    if behavior_pair is not None:
        report["behavior_policy_comparison"] = behavior_pair
        report["interpretation"] = (
            f"Observed paired-run differences across {len(per_seed)} shared synthetic seeds; "
            "profiles, facilities, staff, events, coefficients, and application version match, "
            "with only the installed behavior policy changed. Results are descriptive simulator "
            "behavior, not real-world prediction or evidence that one policy is better."
        )
    return report


def _validate_behavior_policy_pair(
    baseline_path: Path,
    baseline_manifest: dict[str, Any],
    candidate_path: Path,
    candidate_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Allow only the registry policy selector to differ between paired run inputs."""
    baseline_behavior = baseline_manifest.get("model_assumptions", {}).get("behavior", {})
    candidate_behavior = candidate_manifest.get("model_assumptions", {}).get("behavior", {})
    baseline_variant = baseline_behavior.get("variant")
    candidate_variant = candidate_behavior.get("variant")
    registered = {"utility-softmax-v1", "priority-rule-v1"}
    if baseline_variant not in registered or candidate_variant not in registered:
        raise ValueError(
            "policy comparison requires registered behavior variants in both manifests"
        )
    if baseline_variant == candidate_variant:
        raise ValueError("policy comparison requires two different behavior variants")
    if baseline_manifest.get("application_version") != candidate_manifest.get(
        "application_version"
    ):
        raise ValueError("policy variants must use the same application version")
    if baseline_manifest.get("seed") != candidate_manifest.get("seed"):
        raise ValueError("policy variants must use the same seed")

    def normalized_inputs(directory: Path, manifest: dict[str, Any]) -> str:
        model_configs = manifest.get("model_configs")
        if not isinstance(model_configs, dict):
            raise ValueError("policy comparison requires model_configs in both manifests")
        model_configs = json.loads(_canonical_json(model_configs))
        behavior_config = model_configs.get("behavior")
        if not isinstance(behavior_config, dict):
            raise ValueError("policy comparison requires a behavior model config")
        behavior_config.pop("policy", None)
        behavior_config.pop("source_id", None)
        assumptions = json.loads(_canonical_json(manifest.get("model_assumptions", {})))
        assumptions_behavior = assumptions.get("behavior")
        if not isinstance(assumptions_behavior, dict):
            raise ValueError("policy comparison requires behavior assumptions")
        for key in (
            "source_id",
            "variant",
            "policy_version",
            "selection_rule",
            "capabilities",
        ):
            assumptions_behavior.pop(key, None)
        scenario_provenance = json.loads(_canonical_json(manifest.get("scenario_provenance")))
        declared_assumptions = scenario_provenance.get("model_assumption_ids")
        if isinstance(declared_assumptions, list):
            scenario_provenance["model_assumption_ids"] = [
                item
                for item in declared_assumptions
                if item
                not in {
                    "ASM-UTILITY-BEHAVIOR-V1",
                    "ASM-PRIORITY-RULE-BEHAVIOR-V1",
                }
            ]
        profile_input = _read_object(directory / "agents_initial.json")
        profile_input.pop("scenario_id", None)
        planned_input = _read_object(directory / "events_planned.json")
        planned_input.pop("scenario_id", None)
        inputs = {
            "scenario_schema_version": manifest.get("scenario_schema_version"),
            "world_state_schema_version": manifest.get("world_state_schema_version"),
            "duration_minutes": manifest.get("duration_minutes"),
            "seed": manifest.get("seed"),
            "model_configs": model_configs,
            "model_assumptions": assumptions,
            "scenario_provenance": scenario_provenance,
            "initial_stock": manifest.get("initial_stock"),
            "facilities": manifest.get("facilities"),
            "staff": manifest.get("staff"),
            "profiles": profile_input,
            "zones": _read_object(directory / "zones.json"),
            "planned_events": planned_input,
        }
        return hashlib.sha256(_canonical_json(inputs).encode("utf-8")).hexdigest()

    if normalized_inputs(baseline_path, baseline_manifest) != normalized_inputs(
        candidate_path, candidate_manifest
    ):
        raise ValueError("policy comparison inputs differ beyond the declared behavior policy")
    return {
        "baseline_variant": baseline_variant,
        "baseline_version": baseline_behavior.get("policy_version", baseline_variant),
        "baseline_run_id": baseline_manifest.get("run_id"),
        "baseline_scenario_hash": baseline_manifest.get("scenario_hash"),
        "candidate_variant": candidate_variant,
        "candidate_version": candidate_behavior.get("policy_version", candidate_variant),
        "candidate_run_id": candidate_manifest.get("run_id"),
        "candidate_scenario_hash": candidate_manifest.get("scenario_hash"),
    }


def _decision_action_counts(run_directory: Path) -> dict[str, Any]:
    path = run_directory / "decisions.jsonl"
    if not path.is_file():
        return {"available": False, "total": 0, "by_action": {}}
    counts: dict[str, int] = {}
    total = 0
    for record in _read_jsonl(path):
        action = record.get("selected_action", record.get("action"))
        if isinstance(action, str):
            counts[action] = counts.get(action, 0) + 1
            total += 1
    return {"available": True, "total": total, "by_action": dict(sorted(counts.items()))}


def _compare_need_cohorts(baseline_path: Path, candidate_path: Path) -> dict[str, Any]:
    """Compare recorded unmet-need snapshots for declared synthetic support cohorts."""
    baseline = _decision_need_cohorts(baseline_path)
    candidate = _decision_need_cohorts(candidate_path)
    cohorts = {}
    for cohort_id in baseline["cohorts"]:
        before = baseline["cohorts"][cohort_id]
        after = candidate["cohorts"][cohort_id]
        need_names = sorted(set(before["mean_needs"]) & set(after["mean_needs"]))
        cohorts[cohort_id] = {
            "population": before["population"],
            "baseline": before,
            "candidate": after,
            "delta_candidate_minus_baseline": {
                need: after["mean_needs"][need] - before["mean_needs"][need] for need in need_names
            },
        }
    return {
        "measure": (
            "Mean of each Agent's recorded pre-decision need snapshots; Agents receive equal "
            "weight. This is not minute-weighted, measured human data, or a validated "
            "fairness metric."
        ),
        "cohorts": cohorts,
    }


def _decision_need_cohorts(run_directory: Path) -> dict[str, Any]:
    profile_document = _read_object(run_directory / "agents_initial.json")
    profiles = profile_document.get("profiles", [])
    if not isinstance(profiles, list):
        raise ValueError("agents_initial.json has no profile list for need cohort comparison")
    parsed_profiles = [
        item for item in profiles if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    if not parsed_profiles:
        raise ValueError("need cohort comparison requires at least one profile")
    ranked = sorted(
        parsed_profiles,
        key=lambda item: (-float(item.get("support_need", 0.0)), item["id"]),
    )
    upper_quartile_count = max(1, math.ceil(len(ranked) * 0.25))
    cohorts = {
        "highest_support_need_quartile": {item["id"] for item in ranked[:upper_quartile_count]},
        "mobility_support": {
            item["id"] for item in ranked if item.get("mobility") != "independent"
        },
    }
    decisions_path = run_directory / "decisions.jsonl"
    decisions = _read_jsonl(decisions_path) if decisions_path.is_file() else []
    results = {}
    for cohort_id, agent_ids in cohorts.items():
        per_agent: dict[str, dict[str, list[float]]] = {agent_id: {} for agent_id in agent_ids}
        snapshots = 0
        for record in decisions:
            agent_id = record.get("agent_id")
            state = record.get("observed_state")
            needs = state.get("needs") if isinstance(state, dict) else None
            if (
                not isinstance(agent_id, str)
                or agent_id not in agent_ids
                or not isinstance(needs, dict)
            ):
                continue
            valid_values = {
                name: float(value)
                for name, value in needs.items()
                if isinstance(name, str)
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            }
            if valid_values:
                snapshots += 1
            for name, value in valid_values.items():
                per_agent[agent_id].setdefault(name, []).append(value)
        all_names = sorted({name for values in per_agent.values() for name in values})
        mean_needs = {}
        for name in all_names:
            agent_means = [
                statistics.fmean(values[name]) for values in per_agent.values() if name in values
            ]
            if agent_means:
                mean_needs[name] = statistics.fmean(agent_means)
        results[cohort_id] = {
            "population": len(agent_ids),
            "agents_with_recorded_needs": sum(bool(values) for values in per_agent.values()),
            "decision_snapshots": snapshots,
            "mean_needs": mean_needs,
        }
    return {"cohorts": results}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def summarize_seed_comparisons(reports: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize paired numeric metric deltas with mean, spread, and quantiles."""
    rows = list(reports)
    if not rows:
        raise ValueError("at least one paired comparison is required")
    seeds = [row.get("seed") for row in rows]
    if len(seeds) != len(set(seeds)):
        raise ValueError("paired comparison seeds must be unique")
    values_by_metric: dict[str, list[float]] = {}
    for row in rows:
        for metric, difference in row.get("final_metric_differences", {}).items():
            delta = difference.get("delta")
            if isinstance(delta, (int, float)) and not isinstance(delta, bool):
                values_by_metric.setdefault(metric, []).append(float(delta))
    summary = {metric: _distribution(values) for metric, values in sorted(values_by_metric.items())}
    return {
        "schema_version": 1,
        "seed_count": len(rows),
        "seeds": sorted(seeds),
        "paired_delta_summary": summary,
        "interpretation": (
            "Descriptive paired-seed statistics; not a guarantee of real-world effects."
        ),
    }


def compare_sensitivity_run_sets(
    baseline_runs: Iterable[dict[str, Any]],
    variant_runs: Iterable[dict[str, Any]],
    *,
    case_id: str,
    parameter_path: str,
    baseline_value: Any,
    variant_value: Any,
    assumption_id: str,
    model_version: str,
    expected_seeds: Iterable[int] | None = None,
    context_source_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Compare one changed model assumption across the same small set of seeds.

    Unlike an event intervention comparison, the frozen model configuration is expected
    to differ at exactly one declared parameter. The caller supplies a canonical input
    hash with that parameter and the simulation seed replaced by fixed markers.
    """
    if not case_id or not parameter_path.startswith("model_configs."):
        raise ValueError("sensitivity case and model_configs parameter path are required")
    if not assumption_id or not model_version:
        raise ValueError("sensitivity results require an assumption ID and model version")
    if type(baseline_value) is not type(variant_value) or baseline_value == variant_value:
        raise ValueError("sensitivity baseline and variant values must be distinct, matching types")

    def index_runs(
        documents: Iterable[dict[str, Any]], expected_value: Any, condition: str
    ) -> dict[int, dict[str, Any]]:
        indexed: dict[int, dict[str, Any]] = {}
        for document in documents:
            if not isinstance(document, dict):
                raise ValueError("sensitivity run records must be objects")
            if document.get("case_id") != case_id:
                raise ValueError("sensitivity run record belongs to a different case")
            seed = document.get("seed")
            if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
                raise ValueError("sensitivity run seed must be a non-negative integer")
            if seed in indexed:
                raise ValueError(f"sensitivity run set contains duplicate seed {seed}")
            value = document.get("parameter_value")
            if type(value) is not type(expected_value) or value != expected_value:
                raise ValueError(f"{condition} run does not use its declared parameter value")
            for key in (
                "scenario_hash",
                "model_config_hash",
                "final_state_hash",
                "input_hash",
            ):
                if not _is_sha256(document.get(key)):
                    raise ValueError(f"sensitivity run {key} must be a SHA-256 hex digest")
            metrics = document.get("metrics")
            if not isinstance(metrics, dict) or not metrics:
                raise ValueError("sensitivity runs need final numeric metrics")
            for metric, value in metrics.items():
                if (
                    not isinstance(metric, str)
                    or not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                ):
                    raise ValueError("sensitivity metric names and values must be finite numbers")
            indexed[seed] = document
        if len(indexed) < 2:
            raise ValueError("sensitivity analysis requires at least two paired seeds")
        return indexed

    baseline_by_seed = index_runs(baseline_runs, baseline_value, "baseline")
    variant_by_seed = index_runs(variant_runs, variant_value, "variant")
    if baseline_by_seed.keys() != variant_by_seed.keys():
        raise ValueError("sensitivity variants must use the same paired seeds")
    seeds = sorted(baseline_by_seed)
    if expected_seeds is not None:
        requested = list(expected_seeds)
        if (
            any(
                not isinstance(seed, int) or isinstance(seed, bool) or seed < 0
                for seed in requested
            )
            or len(requested) != len(set(requested))
            or seeds != sorted(requested)
        ):
            raise ValueError("sensitivity runs do not match the declared seed list")

    paired = [(seed, baseline_by_seed[seed], variant_by_seed[seed]) for seed in seeds]
    input_hashes = {
        document["input_hash"]
        for _, baseline, variant in paired
        for document in (baseline, variant)
    }
    scenario_ids = {
        document.get("scenario_id")
        for _, baseline, variant in paired
        for document in (baseline, variant)
    }
    metric_names = {
        frozenset(document["metrics"])
        for _, baseline, variant in paired
        for document in (baseline, variant)
    }
    if len(input_hashes) != 1:
        raise ValueError("paired sensitivity runs must have identical inputs except seed/parameter")
    if len(scenario_ids) != 1 or None in scenario_ids:
        raise ValueError("paired sensitivity runs must use the same scenario ID")
    if len(metric_names) != 1:
        raise ValueError("paired sensitivity runs must report the same metrics")

    metrics: dict[str, dict[str, Any]] = {}
    for metric in sorted(next(iter(metric_names))):
        baseline_values = [float(base["metrics"][metric]) for _, base, _ in paired]
        variant_values = [float(candidate["metrics"][metric]) for _, _, candidate in paired]
        deltas = [
            after - before for before, after in zip(baseline_values, variant_values, strict=True)
        ]
        positive = sum(value > 0.0 for value in deltas)
        negative = sum(value < 0.0 for value in deltas)
        zero = len(deltas) - positive - negative
        metrics[metric] = {
            "baseline": _distribution(baseline_values),
            "variant": _distribution(variant_values),
            "paired_delta": {
                **_distribution(deltas),
                "positive_seed_count": positive,
                "negative_seed_count": negative,
                "zero_seed_count": zero,
                "same_nonzero_direction": zero == 0 and (positive == 0 or negative == 0),
            },
        }

    paired_runs = []
    for seed, baseline, variant in paired:
        paired_runs.append(
            {
                "seed": seed,
                "baseline": {
                    "scenario_hash": baseline["scenario_hash"],
                    "model_config_hash": baseline["model_config_hash"],
                    "final_state_hash": baseline["final_state_hash"],
                    "metrics": baseline["metrics"],
                },
                "variant": {
                    "scenario_hash": variant["scenario_hash"],
                    "model_config_hash": variant["model_config_hash"],
                    "final_state_hash": variant["final_state_hash"],
                    "metrics": variant["metrics"],
                },
            }
        )
    return {
        "schema_version": 1,
        "case_id": case_id,
        "scenario_id": next(iter(scenario_ids)),
        "input_hash": next(iter(input_hashes)),
        "parameter": {
            "path": parameter_path,
            "baseline_value": baseline_value,
            "variant_value": variant_value,
            "source_type": "assumption",
            "assumption_id": assumption_id,
            "model_version": model_version,
            "context_source_ids": sorted(set(context_source_ids)),
        },
        "seed_count": len(seeds),
        "seeds": seeds,
        "metrics": metrics,
        "paired_runs": paired_runs,
        "uncertainty_notes": [
            f"Descriptive paired results from {len(seeds)} synthetic simulation seeds.",
            "Dispersion is sample standard deviation and empirical quantiles.",
            "No confidence interval or real-world calibration is claimed.",
            "All non-seed inputs match within this case; only the declared parameter changes.",
        ],
        "interpretation": (
            "Conditional behavior of the configured synthetic simulator, not an estimate of "
            "real shelter outcomes or evidence that one policy is generally better."
        ),
    }


def build_causal_trace(
    decisions: list[dict[str, Any]],
    realized_events: list[dict[str, Any]],
    *,
    focus_agent_id: str | None = None,
    focus_event_id: str | None = None,
) -> dict[str, Any]:
    """Join recorded decisions, event references, facilities, queue waits and staff tasks."""
    nodes: dict[str, CausalNode] = {}
    edge_values: set[tuple[str, str, str, bool]] = set()

    def add_node(node_id: str, kind: str, minute: int | None, **details: Any) -> str:
        nodes.setdefault(node_id, CausalNode(node_id, kind, minute, details))
        return node_id

    def link(source: str, target: str, relationship: str, *, inferred: bool = False) -> None:
        edge_values.add((source, target, relationship, inferred))

    event_rows: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for record in realized_events:
        event = record.get("event")
        if not isinstance(event, dict):
            continue
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            continue
        minute = record.get("realized_minute")
        event_node = add_node(
            f"event:{event_id}",
            "event",
            minute if isinstance(minute, int) else None,
            event_id=event_id,
            event_type=event.get("event_type"),
            status=record.get("status"),
            detail=record.get("detail"),
            payload=event.get("payload", {}),
            provenance=event.get("provenance", {}),
        )
        event_rows.append((record, event, event_node))

    for record, event, event_node in event_rows:
        event_id = event["event_id"]
        event_minute = record.get("realized_minute")
        for reference_field, relationship in (
            ("causation_id", "caused_by"),
            ("source_id", "sourced_from"),
        ):
            reference = event.get(reference_field)
            if isinstance(reference, str) and reference:
                reference_node = (
                    f"event:{reference}"
                    if f"event:{reference}" in nodes
                    else add_node(
                        f"reference:{reference}", "reference", None, reference_id=reference
                    )
                )
                link(reference_node, event_node, relationship)

        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        agent_id = payload.get("agent_id")
        facility_id = payload.get("facility_id")
        staff_id = payload.get("staff_id")
        task_id = payload.get("task_id")
        request_id = payload.get("request_id")
        if isinstance(agent_id, str):
            agent_node = add_node(f"agent:{agent_id}", "agent", None, agent_id=agent_id)
            link(agent_node, event_node, "affected_agent")
        if isinstance(facility_id, str):
            facility_node = add_node(
                f"facility:{facility_id}", "facility", None, facility_id=facility_id
            )
            link(facility_node, event_node, "resource")
        if isinstance(staff_id, str):
            staff_node = add_node(f"staff:{staff_id}", "staff", None, staff_id=staff_id)
            link(staff_node, event_node, "performed_by")
        if isinstance(task_id, str):
            task_node = add_node(f"task:{task_id}", "task", event_minute, task_id=task_id)
            link(task_node, event_node, "task_event")
        if isinstance(request_id, str):
            request_node = add_node(
                f"support-request:{request_id}",
                "support_request",
                event_minute,
                request_id=request_id,
                agent_id=agent_id,
            )
            if isinstance(agent_id, str):
                link(f"agent:{agent_id}", request_node, "requested_by")
            link(request_node, event_node, "request_outcome")

        event_type = event.get("event_type")
        provenance = event.get("provenance", {})
        if (
            event_type == "toilet_service_complete"
            and isinstance(agent_id, str)
            and isinstance(facility_id, str)
            and isinstance(provenance, dict)
            and isinstance(provenance.get("requested_at"), int)
        ):
            requested_at = provenance["requested_at"]
            wait = provenance.get("queue_wait_minutes")
            request_node = add_node(
                f"toilet-request:{facility_id}:{agent_id}:{requested_at}",
                "toilet_request",
                requested_at,
                agent_id=agent_id,
                facility_id=facility_id,
                queue_wait_minutes=wait,
            )
            link(f"agent:{agent_id}", request_node, "requested_by")
            link(f"facility:{facility_id}", request_node, "served_by")
            link(request_node, event_node, "service_started")
            for outage_record, outage_event, outage_node in event_rows:
                outage_payload = outage_event.get("payload", {})
                outage_type = outage_event.get("event_type")
                realized_at = outage_record.get("realized_minute")
                if (
                    isinstance(outage_payload, dict)
                    and outage_payload.get("facility_id") == facility_id
                    and outage_type in ("facility_outage", "facility_recovery")
                    and isinstance(realized_at, int)
                    and realized_at <= requested_at
                ):
                    link(outage_node, request_node, "capacity_context", inferred=True)

    ordered_decisions = sorted(
        enumerate(decisions),
        key=lambda pair: (
            pair[1].get("minute_index", 0),
            str(pair[1].get("agent_id", "")),
            pair[0],
        ),
    )
    for original_index, decision in ordered_decisions:
        agent_id = decision.get("agent_id")
        minute = decision.get("minute_index")
        if not isinstance(agent_id, str) or not isinstance(minute, int):
            continue
        decision_node = add_node(
            f"decision:{minute}:{agent_id}:{original_index}",
            "decision",
            minute,
            agent_id=agent_id,
            action=decision.get("action", decision.get("selected_action")),
            target_id=decision.get("target_id"),
            trigger=decision.get("trigger"),
            selected_probability=decision.get("selected_probability"),
            choice_value=decision.get("choice_value"),
        )
        link(f"agent:{agent_id}", decision_node, "made_by")
        action = decision.get("action", decision.get("selected_action"))
        target_id = decision.get("target_id")
        if action == "seek_help" and isinstance(target_id, str):
            request_node = add_node(
                f"support-request:{target_id}",
                "support_request",
                minute,
                request_id=target_id,
                agent_id=agent_id,
            )
            link(decision_node, request_node, "requested")
        elif action == "request_toilet" and isinstance(target_id, str):
            matching_requests = [
                node
                for node in nodes.values()
                if node.kind == "toilet_request"
                and node.details.get("agent_id") == agent_id
                and node.details.get("facility_id") == target_id
                and node.minute is not None
                and minute <= node.minute
            ]
            if matching_requests:
                nearest_minute = max(
                    node.minute for node in matching_requests if node.minute is not None
                )
                for node in matching_requests:
                    if node.minute == nearest_minute:
                        link(decision_node, node.id, "requested")

    if focus_agent_id is not None:
        focused = {
            node_id
            for node_id, node in nodes.items()
            if node.details.get("agent_id") == focus_agent_id
            or node.details.get("reference_id") == focus_agent_id
        }
        _expand_one_edge(focused, edge_values)
    elif focus_event_id is not None:
        focused = {f"event:{focus_event_id}"}
        _expand_one_edge(focused, edge_values)
    else:
        focused = set(nodes)
    selected_nodes = [nodes[key] for key in sorted(focused) if key in nodes]
    selected_edges = [
        CausalEdge(source, target, relation, inferred)
        for source, target, relation, inferred in sorted(edge_values)
        if source in focused and target in focused
    ]
    return {
        "schema_version": 1,
        "focus_agent_id": focus_agent_id,
        "focus_event_id": focus_event_id,
        "nodes": [asdict(item) for item in selected_nodes],
        "edges": [asdict(item) for item in selected_edges],
        "interpretation": (
            "Recorded references are direct evidence; capacity_context edges are inferred "
            "from matching facility and time only, not causal proof."
        ),
    }


def render_comparison_markdown(report: dict[str, Any]) -> str:
    """Render a compact report that separates observations from interpretation."""
    family_hash = report.get("scenario_family_hash", report.get("scenario_hash", "unknown"))
    lines = [
        "# Paired run comparison",
        "",
        f"- Scenario family hash: `{family_hash}`",
        f"- Baseline scenario: `{report.get('baseline_scenario_id', 'baseline')}`",
        f"- Candidate scenario: `{report.get('candidate_scenario_id', 'candidate')}`",
        f"- Seeds: {report.get('seeds', [report.get('seed', 'unknown')])}",
        "",
    ]
    summary = report.get("paired_delta_summary")
    if isinstance(summary, dict):
        lines.extend(
            [
                "## Paired-seed final metric deltas",
                "",
                "| Metric | Mean | SD | P05 | Median | P95 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for metric, values in sorted(summary.items()):
            lines.append(
                f"| {metric} | {values['mean']:.6g} | {values['stdev']:.6g} "
                f"| {values['p05']:.6g} | {values['median']:.6g} | {values['p95']:.6g} |"
            )
    else:
        lines.extend(
            [
                f"- First observed divergence: {report.get('first_divergence_minute')}",
                "",
                "## Observed final metric differences",
                "",
                "| Metric | Baseline | Candidate | Delta |",
                "|---|---:|---:|---:|",
            ]
        )
        for metric, values in sorted(report.get("final_metric_differences", {}).items()):
            lines.append(
                f"| {metric} | {values.get('baseline')} | {values.get('candidate')} "
                f"| {values.get('delta')} |"
            )
    event_windows = report.get("event_windows")
    if isinstance(event_windows, dict):
        lines.extend(
            [
                "",
                "## Planned event windows",
                "",
                "| Run | Minute | Event | Type | Source | Representative Agents |",
                "|---|---:|---|---|---|---:|",
            ]
        )
        for side in ("baseline", "candidate"):
            for window in event_windows.get(side, []):
                lines.append(
                    f"| {side} | {window.get('minute')} | `{window.get('event_id')}` "
                    f"| {window.get('event_type')} | `{window.get('source_id')}` "
                    f"| {len(window.get('representative_agent_logs', {}).get('after', []))} |"
                )
    lines.extend(["", "## Interpretation", "", str(report.get("interpretation", "")), ""])
    return "\n".join(lines)


def _first_metric_difference(
    before: dict[int, dict[str, Any]], after: dict[int, dict[str, Any]]
) -> tuple[int | None, dict[str, dict[str, Any]]]:
    for minute in sorted(before.keys() | after.keys()):
        left, right = before.get(minute), after.get(minute)
        if left != right:
            return minute, _metric_differences(left or {}, right or {})
    return None, {}


def _metric_differences(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    differences: dict[str, dict[str, Any]] = {}
    for key in sorted(before.keys() | after.keys()):
        left, right = before.get(key), after.get(key)
        if left == right:
            continue
        entry: dict[str, Any] = {"baseline": left, "candidate": right}
        if (
            isinstance(left, (int, float))
            and not isinstance(left, bool)
            and isinstance(right, (int, float))
            and not isinstance(right, bool)
        ):
            entry["delta"] = right - left
        differences[key] = entry
    return differences


def _state_differences(
    before: dict[str, Any],
    after: dict[str, Any],
    project: Any,
    limit: int,
) -> list[dict[str, Any]]:
    if limit == 0:
        return []
    changed: list[dict[str, Any]] = []
    for identifier in sorted(before.keys() | after.keys()):
        left = project(before[identifier]) if identifier in before else None
        right = project(after[identifier]) if identifier in after else None
        if left != right:
            changed.append({"id": identifier, "baseline": left, "candidate": right})
            if len(changed) >= limit:
                break
    return changed


def _distribution(values: list[float]) -> dict[str, float | None]:
    ordered = sorted(values)

    def quantile(probability: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * probability
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        fraction = position - lower
        return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction

    return {
        "mean": statistics.fmean(ordered),
        "stdev": statistics.stdev(ordered) if len(ordered) > 1 else 0.0,
        "p05": quantile(0.05),
        "median": quantile(0.5),
        "p95": quantile(0.95),
    }


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _read_object(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read run artifact {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"run artifact {path} must contain a JSON object")
    return document


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    except OSError as exc:
        raise ValueError(f"could not read metrics {path}: {exc}") from exc


def _run_metric_rows(directory: Path) -> list[dict[str, Any]]:
    rows = [_parse_metric_row(row) for row in _read_csv_rows(directory / "metrics.csv")]
    base_minutes = [row["minute_index"] for row in rows]
    if len(base_minutes) != len(set(base_minutes)):
        raise ValueError(f"run {directory} contains duplicate KPI minutes")
    operational_path = directory / "operational_kpis.csv"
    if not operational_path.exists():
        return rows
    operational_rows = [_parse_metric_row(row) for row in _read_csv_rows(operational_path)]
    by_minute = {row["minute_index"]: row for row in rows}
    operational_minutes = [row["minute_index"] for row in operational_rows]
    if len(operational_minutes) != len(set(operational_minutes)) or set(operational_minutes) != set(
        by_minute
    ):
        raise ValueError(f"run {directory} operational KPI samples do not align with metrics")
    for operational in operational_rows:
        minute = operational["minute_index"]
        row = by_minute.get(minute)
        if row is None:
            raise ValueError(f"run {directory} has operational KPIs without a matching metric")
        for key, value in operational.items():
            if key in row and row[key] != value:
                raise ValueError(f"run {directory} has conflicting KPI field {key!r}")
            row[key] = value
    return rows


def _scenario_compatibility_key(manifest: dict[str, Any]) -> tuple[str, str]:
    family_hash = manifest.get("scenario_family_hash")
    if isinstance(family_hash, str) and family_hash:
        return "scenario_family_hash", family_hash
    definition_hash = manifest.get("scenario_definition_hash", manifest.get("scenario_hash"))
    if isinstance(definition_hash, str) and definition_hash:
        return "scenario_definition_hash", definition_hash
    return "scenario_definition_hash", ""


def _event_windows(
    directory: Path,
    metrics_by_minute: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    planned_path = directory / "events_planned.json"
    if not planned_path.exists():
        return []
    planned = _read_object(planned_path).get("events", [])
    if not isinstance(planned, list):
        return []
    relevant_types = {
        "delivery_delayed",
        "facility_outage",
        "resource_delivery",
        "staff_unavailable",
        "water_outage",
        "water_restored",
    }
    realized: dict[str, dict[str, Any]] = {}
    realized_path = directory / "events_realized.jsonl"
    if realized_path.exists():
        for record in _read_jsonl(realized_path):
            event = record.get("event")
            if isinstance(event, dict) and isinstance(event.get("event_id"), str):
                realized[event["event_id"]] = record
    decisions_path = directory / "decisions.jsonl"
    decisions = _read_jsonl(decisions_path) if decisions_path.exists() else []
    selected: list[dict[str, Any]] = []
    for event in planned:
        if not isinstance(event, dict) or event.get("event_type") not in relevant_types:
            continue
        event_id = event.get("id", event.get("event_id"))
        due_minute = event.get("due_minute")
        if not isinstance(event_id, str) or not isinstance(due_minute, int):
            continue
        record = realized.get(event_id)
        minute = record.get("realized_minute", due_minute) if record else due_minute
        if not isinstance(minute, int):
            minute = due_minute
        before_minute = max(
            (sample for sample in metrics_by_minute if sample < minute), default=None
        )
        after_minute = min(
            (sample for sample in metrics_by_minute if sample >= minute), default=None
        )
        before = metrics_by_minute.get(before_minute) if before_minute is not None else None
        after = metrics_by_minute.get(after_minute) if after_minute is not None else None
        agent_logs: dict[str, list[dict[str, Any]]] = {"before": [], "after": []}
        for side, predicate in (
            ("before", lambda sample: minute - 60 <= sample < minute),
            ("after", lambda sample: minute <= sample <= minute + 60),
        ):
            nearby = [
                decision
                for decision in decisions
                if isinstance(decision.get("minute_index"), int)
                and predicate(decision["minute_index"])
                and isinstance(decision.get("agent_id"), str)
            ]
            relevant = [
                decision
                for decision in nearby
                if decision.get("action") in {"collect_meal", "fetch_water", "seek_help"}
            ]
            candidates = relevant or nearby
            for decision in sorted(
                candidates,
                key=lambda item: (item["minute_index"], item["agent_id"]),
            )[:3]:
                state = decision.get("observed_state")
                agent_logs[side].append(
                    {
                        "minute": decision["minute_index"],
                        "agent_id": decision["agent_id"],
                        "action": decision.get("action"),
                        "target_id": decision.get("target_id"),
                        "observed_state": state if isinstance(state, dict) else None,
                    }
                )
        selected.append(
            {
                "event_id": event_id,
                "event_type": event.get("event_type"),
                "minute": minute,
                "scheduled_minute": due_minute,
                "source_id": event.get("source_id"),
                "causation_id": event.get("causation_id"),
                "realized_status": record.get("status") if record else None,
                "kpi_before": _event_kpi_values(before),
                "kpi_after": _event_kpi_values(after),
                "representative_agent_logs": agent_logs,
            }
        )
    return selected


def _event_kpi_values(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    fields = (
        "hunger_need_mean",
        "hydration_need_mean",
        "water_queue_length",
        "meal_queue_length",
        "water_ml_remaining",
        "food_meal_units_remaining",
        "stress_mean",
        "staff_load_ratio",
        "unresolved_support_request_count",
        "support_no_qualified_worker_count",
        "support_no_qualified_worker_request_minutes",
        "support_fairness_promoted_assignment_count",
        "support_wait_staff_busy_request_minutes",
        "support_wait_staff_off_shift_request_minutes",
    )
    return {key: row[key] for key in fields if key in row}


def _parse_metric_row(row: dict[str, str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in row.items():
        try:
            parsed[key] = int(value) if key == "minute_index" else float(value)
        except (TypeError, ValueError):
            parsed[key] = value
    return parsed


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"could not read event log {path}: {exc}") from exc
    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on {path} line {number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path} line {number} must be an object")
        records.append(row)
    return records


def _expand_one_edge(selected: set[str], edges: set[tuple[str, str, str, bool]]) -> None:
    for _ in range(2):
        neighbors = {
            node
            for source, target, _relationship, _inferred in edges
            if source in selected or target in selected
            for node in (source, target)
        }
        selected.update(neighbors)
