"""Whether a Run writes the same files through the Rust driver and through the Python rules.

The gate compares hashes of the state; the files a Run keeps, its traces among them, are compared
here, byte by byte. Each gate scenario runs in two processes, under SHELTERSIM_NATIVE=1 and =0, and
every file of a Run's folder is compared. The gate selects each model of the registry with no trace,
so each is also run here with a full trace (`--models`). The Rust side says how many ticks its
driver took and which it refused. See docs/rust-port/lanes/l6-trace.md.

    python benchmarks/trace_bytes.py --output DIR [--only NAME] [--models] [--jobs N]
"""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from gate_scenarios import _model_scenario, all_scenarios, selectable_models

from sheltersim import native
from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.runner import run_scenario

GATE = Path(__file__).with_name("gate_all_paths.py")
# What a Run writes for its reader, which holds no time or path.
COMPARED_SUFFIXES = (".jsonl", ".csv", ".json", ".md")
# The trace files of a Run, whose shape is decision D1 of docs/rust-port/step4-plan.md.
TRACE_FILES = (
    "psychology_contributions.jsonl",
    "psychology_contributions_hourly.jsonl",
    "social_contacts.jsonl",
    "information_contacts.jsonl",
    "decisions.jsonl",
    "llm_calls.jsonl",
    "social_decisions.jsonl",
)
# Not a Run's result: the gate's own report of its wall time, and files with the run's own path.
SKIPPED_NAMES = frozenset({"result.json"})


def _run_one(name: str, directory: Path, native: str) -> None:
    directory.mkdir(parents=True)
    env = {**os.environ, "SHELTERSIM_NATIVE": native, "SHELTERSIM_LLM_MODE": "offline"}
    with (directory / "log.txt").open("w", encoding="utf-8") as log:
        subprocess.run(
            [sys.executable, str(GATE), "--run-one", name, "--output", str(directory)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )


def _run_model(domain: str, model: str, level: str, directory: Path) -> None:
    """In its own process, for the mode of its environment."""
    run_scenario(_model_scenario(domain, model), directory / "run", trace_level=TraceLevel(level))
    if native.available():
        counts, reasons = native.rules("tick").stats()
        document = {"ticks": counts["ticks"], "refused": dict(reasons)}
        (directory / "driver.txt").write_text(json.dumps(document), encoding="utf-8")


def _spawn_model(domain: str, model: str, level: str, directory: Path, value: str) -> None:
    directory.mkdir(parents=True)
    env = {**os.environ, "SHELTERSIM_NATIVE": value, "SHELTERSIM_LLM_MODE": "offline"}
    arguments = [domain, model, level, str(directory)]
    with (directory / "log.txt").open("w", encoding="utf-8") as log:
        subprocess.run(
            [sys.executable, __file__, "--run-model", *arguments],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )


def _files(root: Path) -> set[str]:
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and path.suffix in COMPARED_SUFFIXES and path.name not in SKIPPED_NAMES
    }


def compare_model(domain: str, model: str, level: str, output: Path) -> dict:
    name = f"{domain}-{model}-{level}"
    driver, python = output / name / "driver", output / name / "python"
    _spawn_model(domain, model, level, driver, "1")
    _spawn_model(domain, model, level, python, "0")
    result = compare_trees(name, driver, python)
    result["driver"] = json.loads((driver / "driver.txt").read_text(encoding="utf-8"))
    return result


def compare(name: str, output: Path) -> dict:
    driver, python = output / name / "driver", output / name / "python"
    _run_one(name, driver, "1")
    _run_one(name, python, "0")
    return compare_trees(name, driver, python)


def compare_trees(name: str, driver: Path, python: Path) -> dict:
    on_driver, on_python = _files(driver), _files(python)
    different = sorted(
        item
        for item in on_driver & on_python
        if not filecmp.cmp(driver / item, python / item, False)
    )
    sizes: Counter[str] = Counter()
    for item in on_driver & on_python:
        base = Path(item).name
        if base in TRACE_FILES and item not in different:
            sizes[base] += (driver / item).stat().st_size
    return {
        "scenario": name,
        "files": len(on_driver & on_python),
        "only_driver": sorted(on_driver - on_python),
        "only_python": sorted(on_python - on_driver),
        "different": different,
        "trace_bytes": dict(sizes),
    }


def _keeping(name: str, output: Path, run: Callable[[], dict]) -> dict:
    try:
        return run()
    finally:
        if not os.environ.get("TRACE_BYTES_KEEP"):
            shutil.rmtree(output / name, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", action="append", help="scenarios whose name starts with this")
    parser.add_argument("--models", action="store_true", help="each model with a full trace")
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--run-model", nargs=4, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.run_model:
        domain, model, level, directory = args.run_model
        _run_model(domain, model, level, Path(directory))
        return 0
    args.output.mkdir(parents=True, exist_ok=False)
    names = [
        name
        for name in all_scenarios()
        if not args.only or any(name.startswith(prefix) for prefix in args.only)
    ]
    work: list[tuple[str, Callable[[], dict]]] = []
    if args.models:
        for domain, model in selectable_models():
            name = f"{domain}-{model}-full"
            if not args.only or any(name.startswith(prefix) for prefix in args.only):
                work.append((name, partial(compare_model, domain, model, "full", args.output)))
    else:
        work = [(name, partial(compare, name, args.output)) for name in names]
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda item: _keeping(item[0], args.output, item[1]), work))
    totals: Counter[str] = Counter()
    problems = 0
    for result in results:
        totals.update(result["trace_bytes"])
        bad = result["different"] + result["only_driver"] + result["only_python"]
        problems += len(bad)
        driver = result.get("driver")
        said = f", driver {driver['ticks']} ticks, refused {driver['refused']}" if driver else ""
        print(f"{result['scenario']}: {result['files']} files, {len(bad)} differ{said}", flush=True)
        for item in bad[:20]:
            print(f"  {item}", flush=True)
    (args.output / "summary.json").write_text(
        json.dumps(
            {"results": results, "trace_bytes": dict(totals), "problems": problems}, indent=1
        ),
        encoding="utf-8",
    )
    print(
        f"{len(results)} scenarios, {problems} files differ. Trace bytes compared: {dict(totals)}"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
