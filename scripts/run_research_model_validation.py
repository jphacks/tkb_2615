#!/usr/bin/env python3
"""Run deterministic compact ablations or the full 100-Agent, seven-day check."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from sheltersim.core.engine import TraceLevel
from sheltersim.domain.distress_monitor import distress_monitor_summary
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY, information_spread_summary
from sheltersim.domain.psychological_state import PSYCHOLOGICAL_STATE_KEY
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "scenario_100_agents.json"


def _scenario(
    *,
    seed: int,
    profiles: int | None,
    duration: int | None,
    variant: str,
) -> ScenarioSpec:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["id"] = f"research-validation-{variant}-{seed}"
    document["seed"] = seed
    if profiles is not None:
        document["profiles"] = document["profiles"][:profiles]
        for profile in document["profiles"]:
            profile["initial_zone_id"] = "sleeping"
            profile["initial_needs"] = {
                "toilet_urgency": 0.65,
                "hunger": 0.45,
                "fatigue": 0.25,
            }
    if duration is not None:
        document["duration_minutes"] = duration
    if variant == "no-staff-support":
        document["staff"] = []

    is_legacy = variant == "legacy-comparator"
    is_acute_only = variant in {"research-acute-only", "service-long-acute-only"}
    no_correction = variant == "no-correction"
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.01,
        "baseline_distress_by_agent": {},
        "chronic_retention": 0.9,
        "chronic_acute_weight": 0.08,
        "chronic_high_distress_weight": 0.02,
        "high_distress_threshold": 0.7,
    }
    document["model_configs"]["social_propagation"] = {
        "enabled": variant != "no-contagion",
        "max_contacts_per_agent": 3,
        "minimum_contact_minutes": 1 if profiles is not None else 5,
        "stress_spread_per_minute": 0.01 if profiles is not None else 0.001,
        "natural_recovery_per_minute": 0.0,
        "cooldown_minutes": 10,
        "max_pair_stress_increase": 0.05,
        "max_agent_stress_increase_per_minute": 0.05,
        "family_channel_strength": 1.0,
        "same_zone_channel_strength": 0.75,
        "nearby_zone_channel_strength": 0.5,
        "source_id": (
            "ASM-SOCIAL-PROPAGATION-V1" if is_legacy else "ASM-BOSSE-AFFECT-CONTAGION-V1"
        ),
    }
    document["model_configs"]["information_spread"] = {
        "enabled": True,
        "max_contacts_per_agent": 3,
        "minimum_contact_minutes": 1 if profiles is not None else 2,
        "contact_duration_scale_minutes": 5,
        "base_transmission_probability": 0.4,
        "correction_acceptance_probability": 0.7,
        "query_pressure_decay_per_minute": 0.0,
        "correction_source_reliability_by_id": {"a01-staff-source": 0.95},
    }
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": (
            "causal-stress-v1"
            if is_legacy
            else "mean-reverting-distress-v1"
            if is_acute_only
            else "mean-reverting-life-state-v1"
        ),
        "social": "sparse-family-zone-contact-v1"
        if is_legacy
        else "expressiveness-channel-openness-v1",
        "information": "sparse-information-contact-v1" if is_legacy else "evidence-aware-rumor-v1",
    }
    document["model_configs"]["facility_awareness"] = {
        "schema_version": 1,
        "initial_known_facility_ids": sorted(
            facility["id"]
            for facility in document["facilities"]
            if "toilet" in facility["kind"].casefold()
        ),
        "discover_co_located_facilities_on_arrival": True,
        "source_type": "assumption",
        "source_id": "ASM-FACILITY-ENTRY-ORIENTATION-V1",
    }
    rumor_due = 0
    correction_due = min(60, document["duration_minutes"] - 1)
    document["scheduled_events"] = [
        {
            "id": f"a01-rumor-{seed}",
            "kind": "exogenous",
            "due_minute": rumor_due,
            "priority": 0,
            "sequence": 20,
            "event_type": "information_rumor",
            "payload": {
                "message_id": "a01-water-claim",
                "truth_status": "false",
                "origin_agent_id": document["profiles"][0]["id"],
            },
            "source_id": "a01-social-source",
        }
    ]
    if not no_correction:
        document["scheduled_events"].append(
            {
                "id": f"a01-correction-{seed}",
                "kind": "exogenous",
                "due_minute": correction_due,
                "priority": 0,
                "sequence": 21,
                "event_type": "information_correction",
                "payload": {
                    "message_id": "a01-water-claim",
                    "zone_id": "sleeping",
                    "belief": "rejected",
                },
                "source_id": "a01-staff-source",
                "causation_id": f"a01-rumor-{seed}",
            }
        )
    if variant == "recovery-slow":
        document["model_configs"]["psychology"]["natural_recovery_rate_per_minute"] = 0.005
    elif variant == "recovery-fast":
        document["model_configs"]["psychology"]["natural_recovery_rate_per_minute"] = 0.02
    elif variant == "contagion-high":
        document["model_configs"]["social_propagation"]["stress_spread_per_minute"] = 0.03
    elif variant == "support-wait-off":
        document["model_configs"]["psychology"]["support_wait_stress"] = 0.0
    elif variant == "queue-pressure-low":
        document["model_configs"]["psychology"]["queued_stress"] = 0.0
    elif variant == "rumor-low":
        document["model_configs"]["information_spread"]["base_transmission_probability"] = 0.05
    elif variant == "rumor-high":
        document["model_configs"]["information_spread"]["base_transmission_probability"] = 0.8
    elif variant == "rumor-belief-low":
        document["model_configs"]["information_spread"].update(
            rumor_belief_probability=0.0,
            rumor_trust_sensitivity=0.0,
            rumor_source_sensitivity=0.0,
        )
    elif variant == "rumor-belief-high":
        document["model_configs"]["information_spread"].update(
            rumor_belief_probability=1.0,
            rumor_trust_sensitivity=0.0,
            rumor_source_sensitivity=0.0,
        )
    elif variant == "correction-low":
        document["model_configs"]["information_spread"]["correction_acceptance_probability"] = 0.05
        document["model_configs"]["information_spread"]["correction_trust_sensitivity"] = 0.0
        document["model_configs"]["information_spread"]["correction_source_sensitivity"] = 0.0
    elif variant == "correction-high":
        document["model_configs"]["information_spread"]["correction_acceptance_probability"] = 0.95
    elif variant == "temperature-one":
        document["model_configs"].setdefault("behavior", {})["temperature"] = 1.0
    elif variant in {"service-long", "service-long-acute-only"}:
        document["model_configs"]["toilet_service"] = {
            "duration_distribution": "discrete_uniform_inclusive",
            "minimum_minutes": 8,
            "maximum_minutes": 12,
            "source_type": "assumption",
            "source_id": "ASM-TOILET-SERVICE-DURATION-V1",
        }
    return ScenarioSpec.model_validate(document)


def _summary(world: Any, *, variant: str, seed: int, run_dir: Path) -> dict[str, Any]:
    acute = sorted(agent.stress for agent in world.agents.values())
    p95 = acute[max(0, math.ceil(0.95 * len(acute)) - 1)] if acute else 0.0
    decisions_path = run_dir / "decisions.jsonl"
    actions = Counter()
    if decisions_path.exists():
        for line in decisions_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if isinstance(record.get("action"), str):
                actions[record["action"]] += 1
    info = world.model_state.get(INFORMATION_SPREAD_STATE_KEY, {})
    information_summary = (
        information_spread_summary(info, agent_count=len(world.agents))
        if info
        else {}
    )
    summary: dict[str, Any] = {
        "variant": variant,
        "seed": seed,
        "minute": world.minute_index,
        "agent_count": len(acute),
        "acute_distress_mean": statistics.fmean(acute) if acute else 0.0,
        "acute_distress_p95": p95,
        "high_distress_share_ge_0_7": sum(value >= 0.7 for value in acute) / len(acute)
        if acute
        else 0.0,
        "action_records": dict(sorted(actions.items())),
        "information_exposure_count": information_summary.get("exposure_count", 0),
        "wrong_belief_count": information_summary.get("wrong_belief_count", 0),
        "current_wrong_belief_count": information_summary.get(
            "current_wrong_belief_count", 0
        ),
        "ever_wrong_belief_count": information_summary.get("ever_wrong_belief_count", 0),
        "correction_exposure_count": information_summary.get("correction_exposures", 0),
        "correction_acceptance_count": information_summary.get(
            "correction_acceptances", 0
        ),
        "forgotten_information_count": information_summary.get("ever_forgotten_count", 0),
        "currently_forgotten_information_count": information_summary.get(
            "currently_forgotten_count", 0
        ),
        "toilet_units": sum(
            f.working_units for f in world.facilities.values() if "toilet" in f.kind.casefold()
        ),
        "toilet_queue_count": sum(
            len(f.queue_agent_ids)
            for f in world.facilities.values()
            if "toilet" in f.kind.casefold()
        ),
        "service_events_applied": sum(
            1
            for record in world.event_log.records
            if record.status.value == "applied"
            and record.event.event_type == "toilet_service_complete"
        ),
    }
    psychology = world.model_state.get(PSYCHOLOGICAL_STATE_KEY)
    if psychology:
        values = [row["long_term_distress"] for row in psychology["agents"].values()]
        summary["long_term_distress_mean"] = statistics.fmean(values) if values else 0.0
    summary.update(_trace_metrics(run_dir, agent_count=len(acute)))
    summary["online_distress_monitor"] = distress_monitor_summary(world.metric_state)
    return summary


def _trace_metrics(run_dir: Path, *, agent_count: int) -> dict[str, Any]:
    """Derive compact duration, contagion, queue and utilization measures from artifacts."""
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    trace_enabled = manifest.get("trace_level") != TraceLevel.OFF.value
    acute_by_agent: dict[str, list[tuple[int, float]]] = {}
    social_by_minute_agent: dict[tuple[int, str], float] = {}
    social_positive = 0.0
    social_negative = 0.0
    psychology_path = run_dir / "psychology_contributions.jsonl"
    if psychology_path.exists():
        for line in psychology_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            minute = record["minute_index"]
            for row in record["agents"]:
                acute_by_agent.setdefault(row["agent_id"], []).append(
                    (minute, row["stress"]["after"])
                )
    social_path = run_dir / "social_contacts.jsonl"
    if social_path.exists():
        for line in social_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            for contribution in record.get("contributions", []):
                amount = contribution["stress_contribution"]
                social_positive += max(0.0, amount)
                social_negative += max(0.0, -amount)
                key = (record["minute_index"], contribution["recipient_id"])
                social_by_minute_agent[key] = social_by_minute_agent.get(key, 0.0) + amount

    high_runs: list[int] = []
    completed_recovery_runs: list[int] = []
    reascents = 0
    for agent_id, values in acute_by_agent.items():
        active_length = 0
        episodes = 0
        previous_minute: int | None = None
        for minute, base_value in values:
            if previous_minute is not None and minute != previous_minute + 1:
                if active_length:
                    high_runs.append(active_length)
                active_length = 0
            value = min(
                1.0,
                max(
                    0.0,
                    base_value + social_by_minute_agent.get((minute, agent_id), 0.0),
                ),
            )
            if value >= 0.7:
                active_length += 1
                if active_length == 1:
                    episodes += 1
            elif active_length:
                high_runs.append(active_length)
                completed_recovery_runs.append(active_length)
                active_length = 0
            previous_minute = minute
        if active_length:
            high_runs.append(active_length)
        reascents += max(0, episodes - 1)

    metrics_path = run_dir / "metrics.csv"
    with metrics_path.open(encoding="utf-8", newline="") as stream:
        metric_rows = list(csv.DictReader(stream))
    queues = [int(row["toilet_queue_length"]) for row in metric_rows]
    active = [int(row["toilet_active_users"]) for row in metric_rows]
    working = [int(row["toilet_working_units"]) for row in metric_rows]
    wait_means = [float(row["toilet_wait_mean_minutes"]) for row in metric_rows]
    wait_maxima = [float(row["toilet_wait_max_minutes"]) for row in metric_rows]
    long_term_toilets_needed = math.ceil(agent_count / 20)
    has_resource_ledger = bool(metric_rows) and "water_ml_remaining" in metric_rows[0]
    resource_shortage_minutes = None
    if has_resource_ledger:
        resource_shortage_minutes = sum(
            int(row["water_ml_remaining"]) == 0
            or int(row["food_meal_units_remaining"]) == 0
            for row in metric_rows
        )
    return {
        "high_distress_episode_count_ge_0_7": len(high_runs) if trace_enabled else None,
        "high_distress_longest_run_minutes": max(high_runs, default=0) if trace_enabled else None,
        "high_distress_mean_recovery_minutes": (
            None
            if not trace_enabled
            else statistics.fmean(completed_recovery_runs)
            if completed_recovery_runs
            else 0.0
        ),
        "high_distress_reascent_count": reascents if trace_enabled else None,
        "social_positive_stress_influence": social_positive if trace_enabled else None,
        "social_negative_stress_influence_magnitude": (
            social_negative if trace_enabled else None
        ),
        "toilet_queue_mean_length": statistics.fmean(queues) if queues else 0.0,
        "toilet_queue_person_minutes": sum(queues),
        "toilet_wait_mean_minutes_final": wait_means[-1] if wait_means else 0.0,
        "toilet_wait_max_minutes": max(wait_maxima, default=0.0),
        "toilet_utilization_mean": (
            statistics.fmean(
                min(1.0, used / units) for used, units in zip(active, working, strict=True) if units
            )
            if any(working)
            else 0.0
        ),
        "toilet_long_term_planning_shortage_minutes": sum(
            units < long_term_toilets_needed for units in working
        ),
        "resource_shortage_minutes": resource_shortage_minutes,
    }


def run_sweep(output: Path, *, seed_values: tuple[int, ...]) -> dict[str, Any]:
    ablations = (
        "legacy-comparator",
        "research-full",
        "research-acute-only",
        "no-recovery",
        "no-contagion",
        "no-staff-support",
        "no-correction",
    )
    sensitivity_variants = (
        "recovery-slow",
        "recovery-fast",
        "contagion-high",
        "support-wait-off",
        "queue-pressure-low",
        "rumor-low",
        "rumor-high",
        "rumor-belief-low",
        "rumor-belief-high",
        "correction-low",
        "correction-high",
        "temperature-one",
        "service-long-acute-only",
    )
    rows = []
    for seed, variants in [
        *((seed, ablations) for seed in seed_values),
        (seed_values[0], sensitivity_variants),
    ]:
        for variant in variants:
            scenario_variant = "research-full" if variant == "no-recovery" else variant
            scenario = _scenario(seed=seed, profiles=12, duration=180, variant=scenario_variant)
            if variant == "no-recovery":
                document = scenario.model_dump(mode="json")
                document["model_configs"]["psychology"]["natural_recovery_rate_per_minute"] = 0.0
                scenario = ScenarioSpec.model_validate(document)
            with tempfile.TemporaryDirectory(prefix=f"{output.stem}-{variant}-{seed}-") as scratch:
                run_dir = Path(scratch) / "run"
                world = run_scenario(scenario, run_dir, trace_level=TraceLevel.SUMMARY)
                rows.append(_summary(world, variant=variant, seed=seed, run_dir=run_dir))
    return {
        "schema_version": 1,
        "study_id": "research-model-ablation-sensitivity-a02",
        "design": (
            "Matched seeds; 12 synthetic Agents; 180 minutes; legacy causal comparator, "
            "mean-reverting acute-only comparator, integrated mean-reverting life-state "
            "research model, one-factor ablations and sensitivities."
        ),
        "seed_values": list(seed_values),
        "ablation_variants": list(ablations),
        "one_factor_sensitivity_variants": list(sensitivity_variants),
        "results": rows,
        "interpretation": (
            "Internal software sensitivity only; not empirical calibration, validation, "
            "or shelter outcome evidence."
        ),
    }


def run_seven_day(output: Path) -> dict[str, Any]:
    scenario = _scenario(seed=2026, profiles=None, duration=10_080, variant="research-full")
    with tempfile.TemporaryDirectory(prefix=f"{output.stem}-long-") as scratch:
        run_dir = Path(scratch) / "run"
        first_started = time.perf_counter()
        world = run_scenario(scenario, run_dir, trace_level=TraceLevel.OFF)
        first_runtime_seconds = time.perf_counter() - first_started
        first_state = world.to_dict()
        repeat_dir = Path(scratch) / "repeat"
        repeat_started = time.perf_counter()
        repeated_world = run_scenario(scenario, repeat_dir, trace_level=TraceLevel.OFF)
        repeat_runtime_seconds = time.perf_counter() - repeat_started
        repeat_matches = first_state == repeated_world.to_dict()
        if not repeat_matches:
            raise RuntimeError("same-seed seven-day state was not reproducible")
        summary = _summary(world, variant="research-full", seed=2026, run_dir=run_dir)
        state_hash = hashlib.sha256(
            json.dumps(
                first_state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
    summary["same_seed_reproducible"] = repeat_matches
    summary["final_world_state_sha256"] = state_hash
    summary["runtime_seconds_per_run"] = {
        "first": first_runtime_seconds,
        "repeat": repeat_runtime_seconds,
    }
    summary["scenario_hash"] = hashlib.sha256(scenario.canonical_json().encode("utf-8")).hexdigest()
    summary["scenario_id"] = scenario.id
    summary["model_versions"] = scenario.model_configs["policy_registry"]
    summary["model_state_keys"] = sorted(world.model_state)
    summary["checkpoint_state_validation"] = (
        "WorldState.validate completed during every tick and run finalization"
    )
    return {
        "schema_version": 1,
        "study_id": "research-model-seven-day-check-a02",
        "design": (
            "100 synthetic Agents x 10,080 integer-minute ticks per run; two repetitions "
            "with seed 2026; integrated "
            "mean-reverting life-state psychology with body/affect feedback; online "
            "distress monitoring; trace and decision artifacts off."
        ),
        "result": summary,
        "interpretation": "Internal completion and invariant check only; no external validation.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("sweep", "seven-day"), default="sweep")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seeds", type=int, nargs="+", default=(3, 4, 5))
    args = parser.parse_args()
    result = (
        run_sweep(args.output, seed_values=tuple(args.seeds))
        if args.mode == "sweep"
        else run_seven_day(args.output)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
