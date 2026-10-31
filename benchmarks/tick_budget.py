"""Whether a lane of the Rust port stays within its tick budget, on a fork late in a stored Run.

Runs the base tree twice and the lane's tree once at the same time, so all three see the same
load, and compares the thread CPU time of TickEngine.step over the same minutes. The second base
run is the A/A control: its ratio says how far the comparison itself is off. A lane may cost at most
+10% (docs/rust-port.md); speed itself is judged at the milestones.

    python benchmarks/tick_budget.py --base ../base --store STORE --output DIR

--base and --lane are trees with src/ and native/pylib/ (build_native first); the lane defaults to
the tree this script is in. STORE holds the Project and Run to fork. The forks stay in the store
under ids starting with --child-prefix. Exit status: 0 within the limit, 1 over it, 2 a run failed
or the runs' states differ. Output belongs outside the repository, and so does the store.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()


def worker(args: argparse.Namespace) -> None:
    """One run, in the tree on PYTHONPATH: fork, advance, and write what it measured."""
    from sheltersim.core.engine import TickEngine
    from sheltersim.storage.projects import ProjectStore
    from sheltersim.web.projects import ProjectAPI

    measured = {"thread": 0.0, "wall": 0.0, "ticks": 0}
    last_world: list[Any] = []
    step = TickEngine.step

    def timed_step(self: Any) -> Any:
        thread, wall = time.thread_time(), time.perf_counter()
        try:
            return step(self)
        finally:
            measured["thread"] += time.thread_time() - thread
            measured["wall"] += time.perf_counter() - wall
            measured["ticks"] += 1
            last_world[:] = [self.world]

    TickEngine.step = timed_step  # type: ignore[method-assign]
    store = ProjectStore(args.store, max_bytes=1_000_000_000_000)
    api = ProjectAPI(store, recover=False)
    started = time.perf_counter()
    try:
        api.fork(
            args.project,
            args.run,
            args.branch,
            {"minute": args.fork_minute, "child_id": args.child, "approved": True},
        )
        fork_seconds = time.perf_counter() - started
        measured.update(thread=0.0, wall=0.0, ticks=0)
        api.submit(args.project, args.run, args.child, args.fork_minute + args.ticks)
        api.wait_idle()
        ticks = dict(measured)
        result = {
            "side": args.side,
            "tree": str(args.tree),
            "fork_seconds": fork_seconds,
            "thread_seconds": ticks["thread"],
            "tick_wall_seconds": ticks["wall"],
            "ticks": ticks["ticks"],
            "minute": last_world[0].minute_index if last_world else None,
            "state_digest": last_world[0].state_digest() if last_world else None,
        }
    finally:
        api.close()
    args.result.write_text(json.dumps(result), encoding="utf-8")


def _start(args: argparse.Namespace, tree: Path, side: str, number: int) -> dict[str, Any]:
    child = f"{args.child_prefix}{side}-{number}-{os.getpid()}"
    output: Path = args.output / f"round{number}-{side}"
    output.mkdir(parents=True)
    command = [
        sys.executable,
        str(HERE),
        "--worker",
        "--side",
        side,
        "--tree",
        str(tree),
        "--store",
        str(args.store),
        "--project",
        args.project,
        "--run",
        args.run,
        "--branch",
        args.branch,
        "--fork-minute",
        str(args.fork_minute),
        "--ticks",
        str(args.ticks),
        "--child",
        child,
        "--result",
        str(output / "result.json"),
        "--output",
        str(output),
    ]
    environment = {
        **os.environ,
        "PYTHONPATH": f"{tree / 'src'}{os.pathsep}{tree / 'native' / 'pylib'}",
        "SHELTERSIM_LLM_MODE": "offline",
        "SHELTERSIM_PROJECTS_DIR": str(args.store),
        "XDG_STATE_HOME": str(output / "state"),
    }
    log = (output / "log.txt").open("w", encoding="utf-8")
    process = subprocess.Popen(
        command, cwd=tree, env=environment, stdout=log, stderr=subprocess.STDOUT
    )
    return {"process": process, "log": log, "output": output, "side": side, "child": child}


def _round(args: argparse.Namespace, lane: Path, number: int) -> dict[str, Any]:
    runs = [
        _start(args, args.base.resolve(), "base", number),
        _start(args, args.base.resolve(), "control", number),
        _start(args, lane, "lane", number),
    ]
    rows: dict[str, Any] = {}
    failed = False
    for run in runs:
        code = run["process"].wait()
        run["log"].close()
        path = run["output"] / "result.json"
        if code != 0 or not path.is_file():
            print(f"{run['side']} failed, see {run['output'] / 'log.txt'}", flush=True)
            failed = True
            continue
        rows[run["side"]] = json.loads(path.read_text(encoding="utf-8"))
    if failed:
        return {"failed": True}
    base, control, measured = rows["base"], rows["control"], rows["lane"]
    middle = (base["thread_seconds"] + control["thread_seconds"]) / 2
    return {
        "failed": False,
        "base_thread_seconds": base["thread_seconds"],
        "control_thread_seconds": control["thread_seconds"],
        "lane_thread_seconds": measured["thread_seconds"],
        "lane_ratio": measured["thread_seconds"] / middle,
        "control_ratio": control["thread_seconds"] / base["thread_seconds"],
        "ticks": measured["ticks"],
        "minute": measured["minute"],
        "same_ticks": base["ticks"] == control["ticks"] == measured["ticks"],
        "same_state": base["state_digest"] == control["state_digest"] == measured["state_digest"],
        "state_digest": measured["state_digest"],
        "fork_seconds": {side: row["fork_seconds"] for side, row in rows.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, help="the tree to compare with")
    parser.add_argument("--lane", type=Path, default=HERE.parents[1])
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", default="p")
    parser.add_argument("--run", default="r")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--fork-minute", type=int, default=9600)
    parser.add_argument("--ticks", type=int, default=240)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--limit", type=float, default=1.10)
    parser.add_argument("--child-prefix", default="budget-")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--side", help=argparse.SUPPRESS)
    parser.add_argument("--tree", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.store = args.store.resolve()
    args.output = args.output.resolve()
    if args.worker:
        worker(args)
        return 0
    if args.base is None or args.rounds < 3:
        parser.error("--base is required, and at least 3 rounds")
    args.output.mkdir(parents=True, exist_ok=True)
    rounds = []
    for number in range(args.rounds):
        row = _round(args, args.lane.resolve(), number)
        rounds.append(row)
        print(json.dumps({"round": number, **row}), flush=True)
    summary: dict[str, Any] = {"rounds": rounds}
    if any(row["failed"] for row in rounds):
        (args.output / "budget.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        return 2
    lane = statistics.median(row["lane_ratio"] for row in rounds)
    control = statistics.median(row["control_ratio"] for row in rounds)
    same = all(row["same_state"] and row["same_ticks"] for row in rounds)
    summary.update(lane_ratio=lane, control_ratio=control, same_state=same, limit=args.limit)
    (args.output / "budget.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(
        f"lane/base {lane:.3f} (rounds {[round(row['lane_ratio'], 3) for row in rounds]}), "
        f"A/A {control:.3f} (rounds {[round(row['control_ratio'], 3) for row in rounds]}), "
        f"limit {args.limit:.2f}, states {'identical' if same else 'DIFFER'}"
    )
    if not same:
        return 2
    return 0 if lane <= args.limit else 1


if __name__ == "__main__":
    sys.exit(main())
