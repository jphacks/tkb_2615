"""Run scenarios on the test-only tracked mirror and compare them with plain runs.

For each scenario, in its own process: a plain run records a digest per engine, minute, phase and
namespace; a tracked run must match that stream at every phase end, and its documents must match
the shadows its forwarded writes rebuilt. Two more runs test dict order: the physical order of
every dict is reversed at each phase end, and every tracked dict iterates backwards all the time.
The end hashes of all runs must match. The plain run also takes the census.

    python benchmarks/tracked_mirror_gate.py --output DIR
    python benchmarks/tracked_mirror_gate.py --output DIR --gate-dir ../wt-gate/benchmarks

The scenarios come from gate_scenarios.py in --gate-dir, by default this directory; without that
file, from the small built-in list below. Run from the repository root with src and tests on
PYTHONPATH. Output belongs outside the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parent
MODES = ("tracked", "reverse_at_phase_end", "reverse_reads")


def _gate_module(gate_dir: Path) -> Any | None:
    if not (gate_dir / "gate_scenarios.py").is_file():
        return None
    sys.path.insert(0, str(gate_dir))
    import gate_scenarios

    return gate_scenarios


def _builtin() -> dict[str, Callable[[Path], dict[str, dict[str, str]]]]:
    """Fixture Scenarios through run_scenario, for when the gate's scenarios aren't there."""
    from support.paths import FIXTURES

    from sheltersim.experiments.runner import run_scenario
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.storage.projects import digest

    def scenario_run(name: str, minutes: int) -> Callable[[Path], dict[str, dict[str, str]]]:
        def run(directory: Path) -> dict[str, dict[str, str]]:
            document = json.loads((FIXTURES / "scenarios" / f"{name}.json").read_text("utf-8"))
            document["duration_minutes"] = minutes
            world = run_scenario(ScenarioSpec.model_validate(document), directory / "run")
            whole = world.to_dict()
            return {
                "run": {
                    "world_hash": digest(whole),
                    "events_hash": digest(whole["event_log"]),
                    "metrics_hash": "",
                }
            }

        return run

    return {
        path.stem: scenario_run(path.stem, 240)
        for path in sorted((FIXTURES / "scenarios").glob("*.json"))
    }


def scenarios(gate_dir: Path) -> dict[str, Callable[[Path], dict[str, dict[str, str]]]]:
    gate = _gate_module(gate_dir)
    if gate is None:
        return _builtin()

    def outcome_of(scenario: Any) -> Callable[[Path], dict[str, dict[str, str]]]:
        return lambda directory: scenario.run(directory).runs

    return {name: outcome_of(item) for name, item in gate.all_scenarios().items()}


def run_one(name: str, gate_dir: Path, output: Path, check_every: int, census_every: int) -> None:
    """In a scenario's own process."""
    from support.tracked_mirror import Session, patch_worker_processes

    state = output / "state"
    os.environ["SHELTERSIM_PROJECTS_DIR"] = str(state / "projects")
    os.environ["XDG_STATE_HOME"] = str(state / "xdg")
    os.environ["SHELTERSIM_LLM_CACHE_PATH"] = str(state / "llm-tape.json")
    runner = scenarios(gate_dir)[name]
    # A scenario's wait bounds a plain run; the comparisons here make a run several times longer.
    from sheltersim.web.projects import ProjectAPI

    wait_idle = ProjectAPI.wait_idle
    ProjectAPI.wait_idle = lambda api, timeout=None: wait_idle(api)  # type: ignore[method-assign]
    result: dict[str, Any] = {"scenario": name, "native": os.environ.get("SHELTERSIM_NATIVE", "1")}

    def execute(mode: str, session: Session, workers: dict[str, Any] | None) -> dict[str, Any]:
        directory = output / "work" / mode
        directory.mkdir(parents=True)
        reports = output / "workers" / mode
        reports.mkdir(parents=True)
        started = time.perf_counter()
        undo = patch_worker_processes(reports, workers) if workers is not None else None
        row: dict[str, Any] = {}
        try:
            with session:
                row["hashes"] = runner(directory)
        except BaseException as exc:
            row["error"] = "".join(traceback.format_exception(exc))[-3000:]
        finally:
            if undo is not None:
                undo()
        row["seconds"] = round(time.perf_counter() - started, 1)
        row.update(session.report())
        row["workers"] = [
            json.loads(path.read_text("utf-8")) for path in sorted(reports.glob("*.json"))
        ]
        return row

    plain = Session(tracked=False, record=True, census_every=census_every, check_every=check_every)
    result["plain"] = execute(
        "plain", plain, {"tracked": False, "census_every": census_every, "check_every": check_every}
    )
    options: dict[str, dict[str, Any]] = {
        "tracked": {},
        "reverse_at_phase_end": {"reverse_at_phase_end": True},
        "reverse_reads": {"reverse_reads": True},
    }
    for mode in MODES:
        worker_options = {"check_every": check_every, **options[mode]}
        session = Session(expect=plain.stream, **worker_options)
        result[mode] = execute(mode, session, worker_options)
        result[mode]["same_hashes_as_plain"] = result[mode].get("hashes") == result["plain"].get(
            "hashes"
        )
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def _spawn(name: str, arguments: argparse.Namespace) -> dict[str, Any]:
    directory = arguments.output / name
    directory.mkdir(parents=True)
    command = [
        sys.executable,
        __file__,
        "--run-one",
        name,
        "--output",
        str(directory),
        "--gate-dir",
        str(arguments.gate_dir),
        "--check-every",
        str(arguments.check_every),
        "--census-every",
        str(arguments.census_every),
    ]
    with (directory / "log.txt").open("w", encoding="utf-8") as log:
        code = subprocess.run(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "SHELTERSIM_LLM_MODE": "offline"},
            check=False,
        ).returncode
    target = directory / "result.json"
    if not target.is_file():
        return {"scenario": name, "crashed": code}
    return json.loads(target.read_text("utf-8"))


def summarize(results: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    """Lines to read, and the union of the censuses."""
    lines = []
    union: dict[str, Any] = {
        "types": {},
        "ops": {},
        "writes_by_namespace": {},
        "values_forwarded": {},
        "writes_into_list_items": {},
        "shared": {},
        "aliases": {},
        "findings": {},
    }
    for result in results:
        name = result["scenario"]
        if "crashed" in result:
            lines.append(f"{name}: CRASHED with code {result['crashed']}")
            continue
        for mode in ("plain", *MODES):
            row = result[mode]
            _merge(union, row, mode)
            for worker in row["workers"]:
                _merge(union, worker, mode)
            parts = [f"{name} {mode}: {row['seconds']}s"]
            parts.append(f"phases vs plain {row['phases_compared_with_plain']}")
            parts.append(f"vs shadow {row['phases_compared_with_shadow']}")
            parts.append(f"workers {len(row['workers'])}")
            if mode != "plain":
                parts.append("hashes " + ("same" if row["same_hashes_as_plain"] else "DIFFER"))
            drifts = row["drifts"] + [d for w in row["workers"] for d in w["drifts"]]
            if drifts:
                parts.append(f"DRIFT {drifts[0]}")
            if "error" in row:
                parts.append("ERROR " + row["error"].strip().splitlines()[-1][:300])
            lines.append("; ".join(parts))
    return lines, union


def _merge(union: dict[str, Any], row: dict[str, Any], mode: str) -> None:
    census = row["census"]
    for section in ("types", "ops"):
        for key, counts in census[section].items():
            target = union[section].setdefault(key, {})
            for kind, count in counts.items():
                target[kind] = target.get(kind, 0) + count
    if mode == "plain":
        for key, value in census["shared"].items():
            union["shared"][key] = union["shared"].get(key, 0) + value
        for key, value in census["aliases"].items():
            target = union["aliases"].setdefault(key, {"sightings": 0, "held_by": []})
            target["sightings"] += value["sightings"]
            target["held_by"] = (target["held_by"] + value["held_by"])[:4]
    if mode == "tracked":
        for section in ("writes_by_namespace", "values_forwarded", "writes_into_list_items"):
            for key, count in census.get(section, {}).items():
                union[section][key] = union[section].get(key, 0) + count
    for kind, details in row["findings"].items():
        target = union["findings"].setdefault(kind, {})
        for detail, count in details.items():
            target[detail] = target.get(detail, 0) + count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gate-dir", type=Path, default=HERE)
    parser.add_argument("--only", action="append", help="scenarios whose name starts with this")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--check-every", type=int, default=1, help="minutes between full phases")
    parser.add_argument("--census-every", type=int, default=30, help="minutes between walks")
    parser.add_argument("--run-one", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    arguments.gate_dir = arguments.gate_dir.resolve()
    arguments.output = arguments.output.resolve()
    if arguments.run_one:
        run_one(
            arguments.run_one,
            arguments.gate_dir,
            arguments.output,
            arguments.check_every,
            arguments.census_every,
        )
        return 0
    arguments.output.mkdir(parents=True, exist_ok=False)
    names = list(scenarios(arguments.gate_dir))
    if arguments.only:
        names = [n for n in names if any(n.startswith(prefix) for prefix in arguments.only)]
    with ThreadPoolExecutor(arguments.jobs) as pool:
        results = list(pool.map(lambda name: _spawn(name, arguments), names))
    lines, union = summarize(results)
    (arguments.output / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (arguments.output / "census.json").write_text(
        json.dumps(union, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("\n".join(lines))
    bad = [
        line
        for line in lines
        if "DRIFT" in line or "DIFFER" in line or "ERROR" in line or "CRASHED" in line
    ]
    print(f"{len(lines)} rows, {len(bad)} with a problem")
    return 1 if bad else 0


if __name__ == "__main__":
    if str(REPOSITORY / "tests") not in sys.path:
        sys.path.insert(0, str(REPOSITORY / "tests"))
    sys.exit(main())
