"""Build a small saved Project Run kept in fixtures/ to test reading Runs of older models.

The Run is h00_small_replay without scheduled events, 90 minutes long: branch main
saved at minutes 0, 30 and 60, and branch child forked from main at minute 30 and
computed to 60, so both may be continued to 90. Only the files a Run needs to be read are kept, with its history index
(history.sqlite) as the writer left it; the frame cache, the write ledger, locks and
worker leases are left out.

fixtures/saved_runs/engine-v1-small was built with this script (SHELTERSIM_NATIVE=0
and SHELTERSIM_LLM_MODE=offline) in a worktree of 6d5662c, before
Checkpoints recorded the whole-model version (engine:model). It is a Run as every
Run saved before then is: the version it was computed with is the unrecorded version
1 (core/model_version.py). Building it again with later code records the version
of that code, so it is not built again; keep the committed files.

    PYTHONPATH=src python scripts/build_saved_run_fixture.py OUTPUT_DIRECTORY
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.storage.projects import ProjectStore, digest

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"
DURATION = 90
COMPUTED = 60
FORK_MINUTE = 30
# What a reader needs; everything else under the Project is rebuilt or per machine.
KEPT = (
    "project.json",
    "scenarios",
    "runs/r/run.json",
    "runs/r/status.json",
    "runs/r/history.sqlite",
    "runs/r/branches",
    "runs/r/checkpoints",
)
LEFT_OUT = {"worker-lock"}


def build(output: Path) -> None:
    document = json.loads(SCENARIO.read_text(encoding="utf-8"))
    document["duration_minutes"] = DURATION
    document["scheduled_events"] = []
    scenario = ScenarioSpec.from_json(json.dumps(document))
    with tempfile.TemporaryDirectory() as directory:
        store = ProjectStore(Path(directory))
        store.create_project("p")
        store.save_scenario(
            "p", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
        )
        store.create_run("p", "r", scenario.id, 1)
        timeline = TimelineStore(store, "p", "r")
        timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
        timeline.advance("main", COMPUTED)
        timeline.fork("main", FORK_MINUTE, "child")
        timeline.advance("child", COMPUTED)
        project = Path(directory) / "p"
        if output.exists():
            shutil.rmtree(output)
        for kept in KEPT:
            source = project / kept
            target = output / "p" / kept
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(
                    source, target, ignore=lambda _, names: [n for n in names if n in LEFT_OUT]
                )
            else:
                shutil.copy2(source, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output)


if __name__ == "__main__":
    main()
