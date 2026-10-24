"""One isolated benchmark run; invoked by run_benchmarks.py."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.benchmark import build_benchmark_scenario, measure_scenario
from sheltersim.experiments.scenario import ScenarioSpec


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--agent-count", required=True, type=int)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--duration-minutes", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--trace-level", choices=[level.value for level in TraceLevel], required=True
    )
    args = parser.parse_args()

    base = ScenarioSpec.load(args.scenario)
    scenario = build_benchmark_scenario(
        base,
        agent_count=args.agent_count,
        seed=args.seed,
        duration_minutes=args.duration_minutes,
    )
    result = measure_scenario(scenario, args.output, trace_level=TraceLevel(args.trace_level))
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
