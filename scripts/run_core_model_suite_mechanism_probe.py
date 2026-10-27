#!/usr/bin/env python3
"""Exercise social and information propagation under a small synthetic contact scenario."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from sheltersim.contracts.policies import CORE_MODEL_SUITE_V2, model_signatures
from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.model_suite import apply_default_model_suite
from sheltersim.experiments.runner import run_scenario

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_research_model_validation import _scenario, _summary  # noqa: E402


def _state_hash(world: Any) -> str:
    encoded = json.dumps(
        world.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs/mathematical-models/core-model-suite-v2-mechanism-probe.json",
    )
    args = parser.parse_args()

    base = _scenario(
        seed=args.seed,
        profiles=12,
        duration=180,
        variant="research-full",
    )
    candidate = apply_default_model_suite(base, force=True)
    git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="core-model-suite-mechanism-probe-") as scratch:
        scratch_path = Path(scratch)
        rows = {}
        for key, scenario in (("baseline", base), ("candidate", candidate)):
            run_dir = scratch_path / key
            world = run_scenario(scenario, run_dir, trace_level=TraceLevel.SUMMARY)
            summary = _summary(
                world,
                variant=key,
                seed=args.seed,
                run_dir=run_dir,
            )
            summary["scenario_sha256"] = hashlib.sha256(
                scenario.canonical_json().encode("utf-8")
            ).hexdigest()
            summary["final_world_state_sha256"] = _state_hash(world)
            summary["model_signatures"] = model_signatures(scenario.model_configs)
            summary["policy_registry"] = scenario.model_configs.get("policy_registry", {})
            rows[key] = summary

    result = {
        "schema_version": 1,
        "study_id": f"{CORE_MODEL_SUITE_V2}-social-information-mechanism-probe",
        "git_head_at_run": git_head,
        "branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip(),
        "run_conditions": {
            "llm_mode": "offline",
            "seed": args.seed,
            "agents": 12,
            "duration_minutes": 180,
            "rumor_event_minute": 0,
            "correction_event_minute": 60,
            "trace_level": TraceLevel.SUMMARY.value,
        },
        "results": rows,
        "interpretation": (
            "Synthetic mechanism path exercise only. Contact, rumor, correction, and "
            "propagation coefficients are scenario assumptions, not empirical effects."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
