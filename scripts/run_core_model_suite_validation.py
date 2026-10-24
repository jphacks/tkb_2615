#!/usr/bin/env python3
"""Compare the saved research baseline and candidate core suite offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from sheltersim.contracts.policies import CORE_MODEL_SUITE_V2, model_signatures
from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.model_suite import apply_default_model_suite
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_research_model_validation import _summary  # noqa: E402


def _state_hash(world: Any) -> str:
    payload = json.dumps(
        world.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate(seed: int, duration: int, *, candidate_only: bool = False) -> dict[str, Any]:
    document = json.loads(
        (ROOT / "fixtures/scenarios/research_operational_100_7d.json").read_text(
            encoding="utf-8"
        )
    )
    document["id"] = f"core-model-suite-validation-{seed}-{duration}"
    document["seed"] = seed
    document["duration_minutes"] = duration
    document["scheduled_events"] = [
        event for event in document["scheduled_events"] if event["due_minute"] < duration
    ]
    base = ScenarioSpec.model_validate(document)
    candidate = apply_default_model_suite(base, force=True)
    git_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="core-model-suite-validation-") as scratch:
        scratch_path = Path(scratch)
        if candidate_only:
            started = time.perf_counter()
            candidate_world = run_scenario(
                candidate,
                scratch_path / "candidate",
                trace_level=TraceLevel.OFF,
            )
            candidate_seconds = time.perf_counter() - started
            candidate_result = _summary(
                candidate_world,
                variant=CORE_MODEL_SUITE_V2,
                seed=seed,
                run_dir=scratch_path / "candidate",
            )
            agents = candidate_world.model_state.get("body_state_v1", {}).get(
                "agents", {}
            )
            debts = [item["sleep_debt_minutes"] for item in agents.values()]
            candidate_result["body_state"] = {
                "sleep_debt_mean_minutes": sum(debts) / len(debts) if debts else None
            }
            candidate_result["scenario_id"] = candidate.id
            candidate_result["scenario_sha256"] = hashlib.sha256(
                candidate.canonical_json().encode("utf-8")
            ).hexdigest()
            candidate_result["model_signatures"] = model_signatures(
                candidate.model_configs
            )
            candidate_result["policy_registry"] = candidate.model_configs.get(
                "policy_registry", {}
            )
            return {
                "schema_version": 1,
                "study_id": f"{CORE_MODEL_SUITE_V2}-candidate-only",
                "git_head_at_run": git_head,
                "branch": subprocess.check_output(
                    ["git", "branch", "--show-current"], cwd=ROOT, text=True
                ).strip(),
                "run_conditions": {
                    "llm_mode": "offline",
                    "seed": seed,
                    "duration_minutes": duration,
                    "agent_count": len(candidate_world.agents),
                    "trace_level": TraceLevel.OFF.value,
                    "comparison": "candidate only; compare with separate baseline record",
                },
                "result": candidate_result,
                "same_seed_final_state_sha256": _state_hash(candidate_world),
                "runtime_seconds": candidate_seconds,
                "interpretation": (
                    "Candidate-only internal software run. Trace-based episode and social "
                    "contribution metrics are intentionally unavailable; this is not "
                    "external validation."
                ),
            }
        started = time.perf_counter()
        baseline_world = run_scenario(
            base, scratch_path / "baseline", trace_level=TraceLevel.SUMMARY
        )
        baseline_seconds = time.perf_counter() - started
        started = time.perf_counter()
        candidate_world = run_scenario(
            candidate,
            scratch_path / "candidate",
            trace_level=TraceLevel.SUMMARY,
        )
        candidate_seconds = time.perf_counter() - started
        started = time.perf_counter()
        repeated_world = run_scenario(
            candidate,
            scratch_path / "candidate-repeat",
            trace_level=TraceLevel.OFF,
        )
        repeat_seconds = time.perf_counter() - started
        trace_independent = candidate_world.to_dict() == repeated_world.to_dict()
        if not trace_independent:
            raise RuntimeError("Trace SUMMARY and OFF changed the candidate final state")

        baseline = _summary(
            baseline_world,
            variant="saved-research-full",
            seed=seed,
            run_dir=scratch_path / "baseline",
        )
        candidate_result = _summary(
            candidate_world,
            variant=CORE_MODEL_SUITE_V2,
            seed=seed,
            run_dir=scratch_path / "candidate",
        )
        for result, scenario in ((baseline, base), (candidate_result, candidate)):
            body = result.setdefault("body_state", {})
            agents = (
                baseline_world if scenario is base else candidate_world
            ).model_state.get("body_state_v1", {}).get("agents", {})
            debts = [item["sleep_debt_minutes"] for item in agents.values()]
            body["sleep_debt_mean_minutes"] = sum(debts) / len(debts) if debts else None
            result["scenario_id"] = scenario.id
            result["scenario_sha256"] = hashlib.sha256(
                scenario.canonical_json().encode("utf-8")
            ).hexdigest()
            result["model_signatures"] = model_signatures(scenario.model_configs)
            result["policy_registry"] = scenario.model_configs.get("policy_registry", {})

        return {
            "schema_version": 1,
            "study_id": f"{CORE_MODEL_SUITE_V2}-fixed-seed-comparison",
            "git_head_at_run": git_head,
            "branch": subprocess.check_output(
                ["git", "branch", "--show-current"], cwd=ROOT, text=True
            ).strip(),
            "run_conditions": {
                "llm_mode": "offline",
                "seed": seed,
                "duration_minutes": duration,
                "agent_count": len(candidate_world.agents),
                "baseline_trace_level": TraceLevel.SUMMARY.value,
                "candidate_trace_level": TraceLevel.SUMMARY.value,
                "repeat_trace_level": TraceLevel.OFF.value,
            },
            "results": {
                "baseline": baseline,
                "candidate": candidate_result,
                "same_seed_trace_on_off_final_state_equal": trace_independent,
                "candidate_same_seed_final_state_sha256": _state_hash(candidate_world),
            },
            "runtime_seconds": {
                "baseline": baseline_seconds,
                "candidate": candidate_seconds,
                "candidate_repeat": repeat_seconds,
            },
            "interpretation": (
                "Fixed-seed internal software comparison. It checks execution, recorded model "
                "identity, Trace independence and reproducibility; it is not empirical calibration "
                "or external validation."
            ),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--duration", type=int, default=1440)
    parser.add_argument("--candidate-only", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs/mathematical-models/core-model-suite-validation.json",
    )
    args = parser.parse_args()
    result = validate(args.seed, args.duration, candidate_only=args.candidate_only)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
