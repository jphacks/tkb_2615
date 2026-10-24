"""Run the offline Agent-count and seed benchmark matrix in isolated processes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.benchmark import benchmark_document, write_benchmark_document
from sheltersim.experiments.scenario import ScenarioSpec

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIO = ROOT / "fixtures" / "scenarios" / "baseline_100_7d.json"
WORKER = Path(__file__).with_name("_worker.py")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure offline simulation scaling and seed spread."
    )
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--agent-counts", type=int, nargs="+", default=[30, 100, 500, 1000])
    parser.add_argument("--seeds", type=int, nargs="+", default=[2026, 2027])
    parser.add_argument("--duration-minutes", type=int)
    parser.add_argument(
        "--trace-level", choices=[level.value for level in TraceLevel], default="summary"
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--results",
        type=Path,
        help="JSON result file (default: OUTPUT_DIR/benchmark_results.json)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if any(count <= 0 for count in args.agent_counts):
        parser.error("agent counts must be positive")
    if any(seed < 0 for seed in args.seeds):
        parser.error("seeds must be non-negative")
    if len(set(args.agent_counts)) != len(args.agent_counts):
        parser.error("agent counts must be unique")
    if len(set(args.seeds)) != len(args.seeds):
        parser.error("seeds must be unique")
    try:
        base = ScenarioSpec.load(args.scenario)
    except (OSError, ValueError) as exc:
        parser.error(f"could not load benchmark scenario: {exc}")
    duration_minutes = (
        base.duration_minutes if args.duration_minutes is None else args.duration_minutes
    )
    if duration_minutes <= 0:
        parser.error("duration must be positive")

    output_directory = args.output_dir
    output_directory.mkdir(parents=True, exist_ok=True)
    trace_level = TraceLevel(args.trace_level)
    results: list[dict[str, object]] = []
    env = os.environ.copy()
    # The benchmark has no provider integration; keep it independent of any configured API key.
    env.pop("ANTHROPIC_API_KEY", None)
    for count in args.agent_counts:
        for seed in args.seeds:
            run_output = output_directory / f"agents-{count}-seed-{seed}"
            command = [
                sys.executable,
                str(WORKER),
                "--scenario",
                str(args.scenario.resolve()),
                "--agent-count",
                str(count),
                "--seed",
                str(seed),
                "--duration-minutes",
                str(duration_minutes),
                "--output",
                str(run_output),
                "--trace-level",
                trace_level.value,
            ]
            print(f"Running {count} Agents, seed {seed}, {duration_minutes} minutes", flush=True)
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            result = json.loads(completed.stdout)
            results.append(result)
            print(
                f"Completed in {result['wall_seconds']:.2f}s; "
                f"{result['trace_bytes']} trace bytes; "
                f"{result['artifact_bytes']} total artifact bytes",
                flush=True,
            )

    result_path = args.results or output_directory / "benchmark_results.json"
    document = benchmark_document(
        scenario_source=str(args.scenario.resolve()),
        duration_minutes=duration_minutes,
        trace_level=trace_level,
        runs=results,
    )
    write_benchmark_document(document, result_path)
    print(f"Wrote {result_path} and {result_path.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
