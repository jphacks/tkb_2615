"""Offline differential timing of the library and the actual ProjectAPI worker.

Run in a fresh process per case. Output belongs outside the repository.
"""

from __future__ import annotations

import argparse
import cProfile
import functools
import json
import os
import pstats
import socket
import time
from pathlib import Path

os.environ["SHELTERSIM_LLM_MODE"] = "offline"
os.environ.pop("ANTHROPIC_API_KEY", None)

from sheltersim.contracts.policies import model_signatures
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.experiments.benchmark import build_benchmark_scenario, environment_manifest
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore, _IndexedMetricSink
from sheltersim.inspection import frames as inspection_frames
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.storage.checkpoints import CheckpointArchive
from sheltersim.storage.frames import FrameCache
from sheltersim.storage.history_index import HistoryIndex
from sheltersim.storage.index_writer import BackgroundIndexer
from sheltersim.storage.projects import ProjectStore, digest
from sheltersim.web.projects import ProjectAPI
from sheltersim.web.workspace import ProjectWorkspace


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--duration", type=int, required=True)
    parser.add_argument("--route", choices=("library", "project"), required=True)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--scenario", choices=("baseline", "generated"), default="baseline")
    parser.add_argument("--scenario-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--expect",
        type=Path,
        help="an earlier result.json whose world, events and KPI hashes must match",
    )
    parser.add_argument(
        "--from-project",
        type=Path,
        help="a projects directory an earlier --route project run saved; fork its main "
        "branch at --fork-minute and compute --duration more minutes",
    )
    parser.add_argument("--fork-minute", type=int)
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("duration must be positive")
    if (args.from_project is None) != (args.fork_minute is None):
        parser.error("--from-project and --fork-minute go together")
    if args.from_project is not None and (args.route != "project" or args.fork_minute < 0):
        parser.error("--from-project needs --route project and a non-negative --fork-minute")
    environment = environment_manifest()
    args.output.mkdir(parents=True, exist_ok=False)
    # Fail closed even if a future regression constructs a network provider.
    network_attempts = []

    def denied(*_args, **_kwargs):
        network_attempts.append(1)
        raise AssertionError("network forbidden by offline benchmark")

    socket.socket.connect = denied
    late_store = ProjectStore(args.from_project) if args.from_project else None
    scenario = build_benchmark_scenario(
        ScenarioSpec.load("fixtures/scenarios/baseline_100_7d.json"),
        agent_count=100,
        seed=2026,
        duration_minutes=args.duration,
    )
    scenario = scenario.model_copy(update={"id": f"perf-agents100-minutes{args.duration}-seed2026"})
    if args.scenario == "generated":
        from sheltersim.experiments.initial_scenario import (
            InitialScenarioBuilder,
            InitialScenarioRequest,
        )

        scenario = (
            InitialScenarioBuilder()
            .build(
                InitialScenarioRequest(
                    municipality="Synthetic municipality",
                    disaster="earthquake",
                    evacuees=100,
                    duration_days=args.duration / 1440,
                    seed=2026,
                )
            )
            .scenario
        )
    if args.scenario_file:
        document = json.loads(ScenarioSpec.load(args.scenario_file).canonical_json())
        document["duration_minutes"] = args.duration
        document["scheduled_events"] = [
            event for event in document["scheduled_events"] if event["due_minute"] < args.duration
        ]
        scenario = ScenarioSpec.model_validate(document)
    if late_store is not None:
        # The saved Run's own Scenario; the options above only name a new one.
        saved_run = late_store.run("p", "r")
        scenario = late_store.scenario("p", saved_run["scenario_id"], saved_run["scenario_version"])
    environment["model_versions"] = model_signatures(scenario.model_configs)
    (args.output / "scenario.json").write_text(scenario.canonical_json() + "\n")
    totals = {}
    progress = []
    started = time.perf_counter()
    cpu_started = time.thread_time()
    last = started
    store = None

    def wrap(owner, name, label=None):
        original = getattr(owner, name)

        @functools.wraps(original)
        def measured(*values, **kwargs):
            nonlocal last
            key = label or f"{owner.__name__}.{name}"
            if name == "__call__":
                key = "phase." + values[1].value
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                return original(*values, **kwargs)
            finally:
                row = totals.setdefault(
                    key, {"calls": 0, "wall_seconds": 0.0, "thread_seconds": 0.0}
                )
                row["calls"] += 1
                row["wall_seconds"] += time.perf_counter() - wall
                row["thread_seconds"] += time.thread_time() - cpu
                if owner is TimelineStore and name == "record":
                    directory = values[0].store.root
                    progress.append(
                        {
                            "saved_minute": values[1].world.minute_index,
                            "elapsed_seconds": time.perf_counter() - started,
                            "files": sum(1 for p in directory.rglob("*") if p.is_file()),
                        }
                    )
                if owner is TickEngine and values[0].world.minute_index % 30 == 0:
                    now = time.perf_counter()
                    # No filesystem scan in the timed loop: count newly published chunks only.
                    item = {
                        "minute": values[0].world.minute_index,
                        "elapsed_seconds": now - started,
                        "interval_seconds": now - last,
                    }
                    progress.append(item)
                    print(json.dumps(item), flush=True)
                    last = now

        setattr(owner, name, measured)

    for owner, name in (
        (TickEngine, "step"),
        (WorldState, "validate_after_tick"),
        (OfflineSimulationController, "__call__"),
        (_IndexedMetricSink, "record_decision"),
        (_IndexedMetricSink, "record_social_decision"),
        (_IndexedMetricSink, "capture_metric"),
        (TimelineStore, "record"),
        (ProjectStore, "save_branch"),
        *(
            ((ProjectStore, "save_captured_branch"),)
            if hasattr(ProjectStore, "save_captured_branch")
            else ()
        ),
        (ProjectStore, "usage"),
        (
            CheckpointArchive,
            "_encode_validated_document"
            if hasattr(CheckpointArchive, "_encode_validated_document")
            else "encode",
        ),
        (HistoryIndex, "ingest"),
        (ProjectWorkspace, "finish_review"),
        # The parts of a save outside capture and the store's write.
        (TimelineStore, "_cache_frame"),
        (FrameCache, "put"),
        (BackgroundIndexer, "submit"),
        (BackgroundIndexer, "_receive"),
        (ProjectStore, "branch_reference"),
    ):
        wrap(owner, name, "CheckpointArchive.encode" if owner is CheckpointArchive else None)
    wrap(inspection_frames, "world_frame", "world_frame")
    # Preserve the classmethod descriptor while timing capture.
    capture = Checkpoint.capture.__func__
    Checkpoint.capture = capture
    wrap(Checkpoint, "capture")
    Checkpoint.capture = classmethod(Checkpoint.capture)
    wrap(os, "fsync", "os.fsync")
    profiler = cProfile.Profile() if args.profile else None
    recorded_metrics = []
    finalize = RunArtifactWriter.finalize

    def finalize_and_observe(writer, world):
        finalize(writer, world)
        recorded_metrics.append(writer.metrics)

    RunArtifactWriter.finalize = finalize_and_observe

    if args.route == "library":
        if profiler:
            profiler.enable()
        world = run_scenario(scenario, args.output / "run", trace_level=TraceLevel.OFF)
        if profiler:
            profiler.disable()
        cpu_seconds = time.thread_time() - cpu_started
        wall_seconds = time.perf_counter() - started
        metrics = recorded_metrics[0].to_dict()
        api_requests = 0
    else:
        fork_seconds = None
        if late_store is not None:
            # A late window: the saves and ticks of a long Run without computing it again.
            store = late_store
            api = ProjectAPI(store, recover=False)
            branch, target = f"late-{args.fork_minute}", args.fork_minute + args.duration
            fork_started = time.perf_counter()
            api.fork(
                "p", "r", "main", {"minute": args.fork_minute, "child_id": branch, "approved": True}
            )
            fork_seconds = time.perf_counter() - fork_started
        else:
            store = ProjectStore(args.output / "projects")
            api = ProjectAPI(store, recover=False)
            branch, target = "main", args.duration
            store.create_project("p")
            store.save_scenario(
                "p", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
            )
            api.post(
                ["p", "runs"],
                {
                    "run_id": "r",
                    "scenario_id": scenario.id,
                    "scenario_version": 1,
                    "execution_mode": "offline",
                },
            )
        setup_seconds = time.perf_counter() - started
        setup_timings = dict(totals)
        totals.clear()
        progress.clear()
        original_advance = api._advance
        worker_cpu = []

        def advance(*values, **kwargs):
            before = time.thread_time()
            if profiler:
                profiler.enable()
            try:
                return original_advance(*values, **kwargs)
            finally:
                if profiler:
                    profiler.disable()
                worker_cpu.append(time.thread_time() - before)

        api._advance = advance
        started = last = time.perf_counter()
        # Match /demo-v2, which resumes without the optional telemetry recording.
        api.submit("p", "r", branch, target)
        api._pool.submit(lambda: None).result()
        wall_seconds = time.perf_counter() - started
        cpu_seconds = worker_cpu[0]
        api.close()
        reference, checkpoint = store.load_branch("p", "r", branch)
        assert reference.computed_until == target, "the computation stopped early"
        world = checkpoint.restore_world()
        metrics = checkpoint.metrics_state
        api_requests = checkpoint.runtime_state["llm"]["api_request_count"]
    result = {
        "environment": environment,
        "route": args.route,
        "duration": args.duration,
        "scenario_hash": digest(json.loads(scenario.canonical_json())),
        "seed": scenario.seed,
        "agent_count": 100,
        "model_configs": scenario.model_configs,
        "trace_level": "off",
        "profiled": args.profile,
        "wall_seconds": wall_seconds,
        "thread_seconds": cpu_seconds,
        "setup_seconds": locals().get("setup_seconds"),
        "late_window": (
            {"fork_minute": args.fork_minute, "fork_seconds": fork_seconds}
            if args.from_project
            else None
        ),
        "setup_timings": locals().get("setup_timings"),
        "scenario_kind": args.scenario,
        "world_hash": digest(world.to_dict()),
        "events_hash": digest(world.to_dict()["event_log"]),
        "metrics_hash": digest(metrics),
        "api_requests": api_requests,
        "network_attempts": len(network_attempts),
        "timings": totals,
        "progress": progress,
    }
    if store:
        result["storage"] = store.usage()
    if profiler:
        profiler.dump_stats(str(args.output / "profile.pstats"))
        with (args.output / "profile.txt").open("w") as stream:
            pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(100)
    import gzip

    (args.output / "final-world.json.gz").write_bytes(
        gzip.compress(world.canonical_json().encode())
    )
    mismatched = []
    if args.expect:
        expected = json.loads(args.expect.read_text(encoding="utf-8"))
        mismatched = [
            key
            for key in ("world_hash", "events_hash", "metrics_hash")
            if expected.get(key) != result[key]
        ]
        result["expected_hashes"] = {"source": str(args.expect), "mismatched": mismatched}
    (args.output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "route",
                    "duration",
                    "wall_seconds",
                    "thread_seconds",
                    "world_hash",
                    "metrics_hash",
                )
            }
        )
    )
    if mismatched:
        raise SystemExit(f"hashes differ from {args.expect}: {', '.join(mismatched)}")


if __name__ == "__main__":
    main()
