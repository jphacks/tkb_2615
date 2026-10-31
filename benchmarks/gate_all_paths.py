"""The all-paths gate: small offline scenarios that together reach most of the tick's code.

Each scenario runs in its own process and writes the world, events and KPI hashes of every
world it ended with, plus timing. Hashes are compared with --expect; --determinism also
runs every scenario under other process settings and compares them with the first run.

    python benchmarks/gate_all_paths.py --output DIR --expect fixtures/gate-all-paths.json
    python benchmarks/gate_all_paths.py --output DIR --determinism
    python benchmarks/gate_all_paths.py --output DIR --reversed-order
    python benchmarks/gate_all_paths.py --output DIR --coverage
    python benchmarks/gate_all_paths.py --output DIR --write-expected fixtures/gate-all-paths.json

Run from the repository root with src on PYTHONPATH. Output belongs outside the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HASHES = ("world_hash", "events_hash", "metrics_hash")
# The code the tick runs; coverage is reported for these.
TICK_SIDE = (
    "sheltersim/core/",
    "sheltersim/domain/",
    "sheltersim/events/",
    "sheltersim/policies/",
    "sheltersim/experiments/runner.py",
)


@dataclass(frozen=True)
class Variant:
    """One way to run a scenario's process."""

    name: str
    env: dict[str, str]
    # Only worth running where Rust's parallel code runs.
    only_with_saves: bool = False


# PYTHONHASHSEED varies set and str-hash order; RAYON_NUM_THREADS varies the parallel parse
# of saved records; SHELTERSIM_NATIVE=0 runs the Python implementation of the Rust parts.
BASE = Variant("base", {"PYTHONHASHSEED": "1"})
VARIANTS = (
    Variant("second_process", {"PYTHONHASHSEED": "1"}),
    Variant("hash_seed", {"PYTHONHASHSEED": "271828"}),
    Variant("one_worker", {"PYTHONHASHSEED": "2", "RAYON_NUM_THREADS": "1"}, only_with_saves=True),
    Variant(
        "four_workers", {"PYTHONHASHSEED": "3", "RAYON_NUM_THREADS": "4"}, only_with_saves=True
    ),
    Variant("python_kernels", {"PYTHONHASHSEED": "4", "SHELTERSIM_NATIVE": "0"}),
)
# With the base run's hash seed, so that only the order of the dicts differs from it.
ORDER_VARIABLE = "SHELTERSIM_GATE_REVERSE"
ORDER_VARIANTS = (
    Variant("reversed_order", {"PYTHONHASHSEED": "1", ORDER_VARIABLE: "phase"}),
    Variant("reversed_within_phase", {"PYTHONHASHSEED": "1", ORDER_VARIABLE: "within_phase"}),
)


def run_one(name: str, output: Path, chosen: Any = None) -> None:
    """In a scenario's own process: run it and write result.json. Fails closed on network.

    ``chosen`` is a GateScenario to run instead of the one by that name.
    """
    import socket

    os.environ["SHELTERSIM_LLM_MODE"] = "offline"
    os.environ.pop("ANTHROPIC_API_KEY", None)
    state = output / "state"
    os.environ["SHELTERSIM_PROJECTS_DIR"] = str(state / "projects")
    os.environ["XDG_STATE_HOME"] = str(state / "xdg")
    os.environ["SHELTERSIM_LLM_CACHE_PATH"] = str(state / "llm-tape.json")

    def denied(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("network forbidden by the offline gate")

    socket.socket.connect = denied  # type: ignore[method-assign]
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gate_scenarios import all_scenarios

    from sheltersim import native

    scenario = chosen or all_scenarios()[name]
    order = os.environ.get(ORDER_VARIABLE)
    if order:
        from tick_probe import ReversedOrder, install

        install([ReversedOrder(within_phase=order == "within_phase")])
    started = time.perf_counter()
    outcome = scenario.run(output / "work")
    mismatched = [
        {"first": first, "second": second, "means": means}
        for first, second, means in outcome.must_match
        if any(outcome.runs[first][key] != outcome.runs[second][key] for key in HASHES)
    ]
    (output / "result.json").write_text(
        json.dumps(
            {
                "scenario": name,
                "what": scenario.what,
                "wall_seconds": time.perf_counter() - started,
                # A comparison with the Python kernels means nothing if both runs used one kind.
                "native_kernels": native.available(),
                "runs": outcome.runs,
                "refused": outcome.refused,
                "must_match": [list(row) for row in outcome.must_match],
                "must_match_failed": mismatched,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _spawn(
    name: str, directory: Path, variant: Variant, coverage_file: Path | None
) -> dict[str, Any]:
    """Run a scenario in a new process; a crash is a result, with its log kept."""
    directory.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, **variant.env, "SHELTERSIM_LLM_MODE": "offline"}
    command = [sys.executable]
    if coverage_file is not None:
        rc = directory / "coverage.rc"
        rc.write_text("[run]\nsource = sheltersim\nbranch = False\ncore = sysmon\n", "utf-8")
        command += ["-m", "coverage", "run", f"--rcfile={rc}", f"--data-file={coverage_file}"]
    command += [__file__, "--run-one", name, "--output", str(directory)]
    started = time.perf_counter()
    with (directory / "log.txt").open("w", encoding="utf-8") as log:
        code = subprocess.run(
            command, env=env, stdout=log, stderr=subprocess.STDOUT, check=False
        ).returncode
    result_file = directory / "result.json"
    if code != 0 or not result_file.exists():
        tail = (directory / "log.txt").read_text(encoding="utf-8")[-2000:]
        return {"scenario": name, "crashed": True, "exit_code": code, "log_tail": tail}
    result = json.loads(result_file.read_text(encoding="utf-8"))
    result["process_seconds"] = time.perf_counter() - started
    return result


def _scenario_names(only: list[str] | None) -> list[str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gate_scenarios import all_scenarios

    names = list(all_scenarios())
    if not only:
        return names
    chosen = [name for name in names if any(name.startswith(prefix) for prefix in only)]
    if not chosen:
        raise SystemExit(f"no scenario matches {only}")
    return chosen


def _run_all(
    names: list[str],
    output: Path,
    variant: Variant,
    jobs: int,
    coverage_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    def one(name: str) -> dict[str, Any]:
        data = coverage_dir / f"{variant.name}.{name}.cov" if coverage_dir else None
        result = _spawn(name, output / variant.name / name, variant, data)
        state = "CRASH" if result.get("crashed") else f"{result['wall_seconds']:.1f}s"
        print(f"[{variant.name}] {name}: {state}", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return dict(zip(names, pool.map(one, names), strict=True))


def _problems_within(results: dict[str, dict[str, Any]]) -> list[str]:
    problems = []
    for name, result in results.items():
        if result.get("crashed"):
            problems.append(f"{name}: crashed (exit {result['exit_code']}): {result['log_tail']}")
            continue
        for row in result["must_match_failed"]:
            problems.append(
                f"{name}: {row['first']} and {row['second']} differ, but {row['means']}"
            )
    return problems


def _differences(label: str, first: dict[str, Any], second: dict[str, Any]) -> list[str]:
    """What differs between two results of one scenario, by run and hash."""
    if first.get("crashed") or second.get("crashed"):
        return [f"{label}: crashed"]
    found = []
    for run in sorted(set(first["runs"]) | set(second["runs"])):
        a, b = first["runs"].get(run), second["runs"].get(run)
        if a is None or b is None:
            found.append(f"{label}/{run}: present on one side only")
            continue
        different = [key for key in HASHES if a[key] != b[key]]
        if different:
            found.append(f"{label}/{run}: {', '.join(different)}")
    if first.get("refused") != second.get("refused"):
        found.append(f"{label}: the refused interventions differ")
    return found


def _expected_document(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scenarios": {
            name: {"runs": result["runs"], "refused": result["refused"]}
            for name, result in sorted(results.items())
        },
    }


def _tick_side_rows(data_files: list[Path]) -> list[dict[str, Any]]:
    """Per file: the tick-side lines that can run and those that did, over all data files."""
    import coverage

    lines: dict[str, set[int]] = {}
    for data_file in data_files:
        part = coverage.CoverageData(basename=str(data_file))
        part.read()
        for filename in part.measured_files():
            lines.setdefault(filename, set()).update(part.lines(filename) or ())
    measured = coverage.Coverage(data_file=None, config_file=False, source=["sheltersim"])
    measured.get_data().add_lines({name: sorted(found) for name, found in lines.items()})
    rows = {}
    root = Path(__import__("sheltersim").__file__).parent
    # Files no scenario imported count as not reached at all.
    for path in sorted(root.rglob("*.py")):
        short = "sheltersim/" + path.relative_to(root).as_posix()
        if short.startswith(TICK_SIDE):
            _, statements, _, missing, _ = measured.analysis2(str(path))
            rows[short] = {
                "file": short,
                "statements": len(statements),
                "missing": len(missing),
                "covered": len(statements) - len(missing),
                "missing_lines": missing,
            }
    return [rows[key] for key in sorted(rows)]


def _write_coverage(rows: list[dict[str, Any]], path: Path) -> None:
    total = sum(row["statements"] for row in rows)
    covered = sum(row["covered"] for row in rows)
    lines = [
        "| file | lines | reached | % |",
        "|---|---:|---:|---:|",
        *(
            f"| {row['file']} | {row['statements']} | {row['covered']} | "
            f"{100 * row['covered'] / max(row['statements'], 1):.0f} |"
            for row in rows
        ),
        f"| total | {total} | {covered} | {100 * covered / max(total, 1):.1f} |",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    print(f"tick-side coverage: {covered}/{total} lines ({100 * covered / max(total, 1):.1f}%)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect", type=Path, help="fixture of expected hashes to compare with")
    parser.add_argument("--write-expected", type=Path, help="write the results as that fixture")
    parser.add_argument("--determinism", action="store_true", help="also run the other variants")
    parser.add_argument(
        "--reversed-order",
        action="store_true",
        help="also reverse model_state's dicts and world.agents at each phase's end, and in phases",
    )
    parser.add_argument(
        "--no-python-kernels",
        action="store_true",
        help="skip the comparison with the Python kernels (Rust and Python agree on Linux only)",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="measure the tick-side lines reached, with the Rust kernels and without",
    )
    parser.add_argument("--only", action="append", help="scenarios whose name starts with this")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 12))
    parser.add_argument("--run-one", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.run_one:
        run_one(args.run_one, args.output)
        return 0

    args.output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    names = _scenario_names(args.only)
    coverage_dir = args.output / "coverage" if args.coverage else None
    if coverage_dir:
        coverage_dir.mkdir()
    results = _run_all(names, args.output, BASE, args.jobs, coverage_dir)
    problems = _problems_within(results)
    summary: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "scenarios": {
            name: {
                "wall_seconds": result.get("wall_seconds"),
                "native_kernels": result.get("native_kernels"),
                "what": result.get("what"),
                "runs": result.get("runs"),
                "refused": result.get("refused"),
                "crashed": bool(result.get("crashed")),
            }
            for name, result in results.items()
        },
    }

    if args.expect:
        expected = json.loads(args.expect.read_text(encoding="utf-8"))["scenarios"]
        for name in names:
            if name not in expected:
                problems.append(f"{name}: not in {args.expect}")
            else:
                problems.extend(_differences(f"expected {name}", expected[name], results[name]))

    if args.determinism or args.coverage or args.reversed_order:
        table: dict[str, dict[str, Any]] = {name: {} for name in names}
        reads_saves = _reads_saves(names)
        variants = [
            variant
            for variant in VARIANTS
            if (args.determinism or (args.coverage and variant.name == "python_kernels"))
            and not (args.no_python_kernels and variant.name == "python_kernels")
        ]
        if args.reversed_order:
            variants += ORDER_VARIANTS
        for variant in variants:
            chosen = [n for n in names if reads_saves[n] or not variant.only_with_saves]
            other = _run_all(chosen, args.output, variant, args.jobs, coverage_dir)
            problems.extend(_problems_within(other))
            for name in chosen:
                differences = _differences(f"{variant.name} {name}", results[name], other[name])
                if (
                    variant.name == "python_kernels"
                    and not other[name].get("crashed")
                    and other[name]["native_kernels"]
                ):
                    differences.append(f"{name}: SHELTERSIM_NATIVE=0 still used the Rust kernels")
                problems.extend(differences)
                table[name][variant.name] = "same" if not differences else "DIFFERENT"
        summary["determinism"] = table

    if coverage_dir:
        native = sorted(coverage_dir.glob(f"{BASE.name}.*.cov"))
        everything = sorted(coverage_dir.glob("*.cov"))
        _write_coverage(_tick_side_rows(native), args.output / "coverage-native.md")
        _write_coverage(_tick_side_rows(everything), args.output / "coverage-both.md")

    if args.write_expected:
        if problems:
            print("not writing expected hashes: there are problems", file=sys.stderr)
        else:
            args.write_expected.write_text(
                json.dumps(_expected_document(results), ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8",
            )

    summary["problems"] = problems
    summary["total_seconds"] = time.perf_counter() - started
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{len(names)} scenarios in {summary['total_seconds']:.1f}s; {len(problems)} problems")
    for problem in problems:
        print(f"PROBLEM {problem}")
    return 1 if problems else 0


def _reads_saves(names: list[str]) -> dict[str, bool]:
    from gate_scenarios import all_scenarios

    scenarios = all_scenarios()
    return {name: scenarios[name].reads_saves for name in names}


if __name__ == "__main__":
    raise SystemExit(main())
