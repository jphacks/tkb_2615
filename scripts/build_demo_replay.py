"""Build the deterministic, offline Day 4 replay used for the local demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from sheltersim.core.engine import TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.experiments.comparison import compare_run_artifacts
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.llm.provider import LLMMode, LLMRouter, LLMSettings
from sheltersim.web.demo import DemoService

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "fixtures" / "scenarios" / "baseline_100_7d.json"
FORK_MINUTE = 3 * 24 * 60 + 14 * 60
ADVANCE_CHUNK = 1_000
BASELINE_RUN_ID = "baseline"
CANDIDATE_RUN_ID = "day4-toilet-outage"
IDEMPOTENCY_KEY = "demo-replay-day4-toilet-outage-v1"
PROMPT = "Day 4 14:00 toilet_1 outage"


def build_replay(output_directory: Path) -> dict[str, Any]:
    """Run a same-checkpoint baseline and approved Day 4 outage branch offline."""
    scenario = ScenarioSpec.load(SCENARIO_PATH)
    if len(scenario.profiles) != 100 or scenario.duration_minutes != 10_080:
        raise ValueError("the packaged replay requires the frozen 100-Agent, seven-day scenario")
    if scenario.duration_minutes <= FORK_MINUTE:
        raise ValueError("the Day 4 fork must occur before the scenario ends")
    if not any(item.id == "toilet_1" for item in scenario.facilities):
        raise ValueError("the packaged scenario has no toilet_1 facility")

    demo = DemoService(scenario)
    demo.compiler_router = LLMRouter(LLMSettings(mode=LLMMode.OFFLINE))
    demo.dispatch("start")
    _advance_to(demo, BASELINE_RUN_ID, FORK_MINUTE)
    demo.dispatch("pause", {"branch_id": BASELINE_RUN_ID})
    demo.dispatch("checkpoint", {"branch_id": BASELINE_RUN_ID})

    preview = demo.interpret_natural_language(
        {
            "text": PROMPT,
            "branch_id": BASELINE_RUN_ID,
            "selected_minute": FORK_MINUTE,
        }
    )
    if preview["result"]["status"] != "compiled" or not preview["validation"]["valid"]:
        raise RuntimeError(f"Day 4 outage preview was not valid: {preview['result']}")
    approval = demo.apply_natural_language_preview(
        {
            "source_branch_id": BASELINE_RUN_ID,
            "preview_id": preview["preview_id"],
            "idempotency_key": IDEMPOTENCY_KEY,
        }
    )
    candidate_branch_id = approval["branch_id"]
    if approval["idempotent_replay"]:
        raise RuntimeError("a fresh replay build unexpectedly reused an existing branch")

    _complete(demo, BASELINE_RUN_ID, candidate_branch_id)
    baseline = demo.branches[BASELINE_RUN_ID].runtime
    candidate = demo.branches[candidate_branch_id].runtime
    if baseline.world.minute_index != scenario.duration_minutes:
        raise RuntimeError("baseline did not complete the full replay")
    if candidate.world.minute_index != scenario.duration_minutes:
        raise RuntimeError("Day 4 branch did not complete the full replay")

    output_directory.mkdir(parents=True, exist_ok=True)
    baseline_path = output_directory / BASELINE_RUN_ID
    candidate_path = output_directory / CANDIDATE_RUN_ID
    _write_branch_artifacts(
        scenario,
        baseline,
        baseline_path,
        branch_id=BASELINE_RUN_ID,
        parent_branch_id=None,
        lineage={
            "schema_version": 1,
            "branch_id": BASELINE_RUN_ID,
            "scenario_id": scenario.id,
            "seed": scenario.seed,
            "state_hash": _world_hash(baseline.world),
        },
    )
    plan_record = candidate.runtime_state["approved_plan_v1"]
    _write_branch_artifacts(
        scenario,
        candidate,
        candidate_path,
        branch_id=candidate_branch_id,
        parent_branch_id=BASELINE_RUN_ID,
        lineage={
            "schema_version": 1,
            "branch_id": candidate_branch_id,
            "parent_branch_id": BASELINE_RUN_ID,
            "source_branch_id": BASELINE_RUN_ID,
            "source_checkpoint_id": plan_record["source_checkpoint_id"],
            "fork_at_minute": FORK_MINUTE,
            "scenario_id": scenario.id,
            "seed": scenario.seed,
            "state_hash": _world_hash(candidate.world),
        },
    )

    return _write_comparison(
        scenario,
        output_directory,
        preview_mode=preview["result"]["support_mode"],
        api_request_count=preview["result"]["usage"]["api_request_count"],
    )


def _write_comparison(
    scenario: ScenarioSpec,
    output_directory: Path,
    *,
    preview_mode: str = "native",
    api_request_count: int = 0,
) -> dict[str, Any]:
    """Refresh the compact card summary from already-generated artifact runs."""
    baseline_path = output_directory / BASELINE_RUN_ID
    candidate_path = output_directory / CANDIDATE_RUN_ID
    if (
        not (baseline_path / "manifest.json").is_file()
        or not (candidate_path / "manifest.json").is_file()
    ):
        raise FileNotFoundError("build both replay runs before refreshing comparison.json")
    candidate_manifest = json.loads((candidate_path / "manifest.json").read_text(encoding="utf-8"))
    comparison = compare_run_artifacts(baseline_path, candidate_path)
    comparison.pop("baseline_run", None)
    comparison.pop("candidate_run", None)
    # Event IDs are allocated after the intervention and then differ for thousands of
    # downstream events. Full event evidence remains in each run; the card needs KPI facts.
    comparison.pop("added_event_ids", None)
    comparison.pop("removed_event_ids", None)
    comparison.update(
        {
            "execution_mode": "precomputed_replay",
            "baseline_run_id": BASELINE_RUN_ID,
            "candidate_run_id": CANDIDATE_RUN_ID,
            "baseline_branch_id": BASELINE_RUN_ID,
            "candidate_branch_id": candidate_manifest["branch_id"],
            "fork_minute": FORK_MINUTE,
            "agent_count": len(scenario.profiles),
            "duration_minutes": scenario.duration_minutes,
            "preview_mode": preview_mode,
            "api_request_count": api_request_count,
            "note": (
                "Fixed offline replay of a synthetic scenario. The single-seed comparison is "
                "descriptive and is not a prediction or evidence of real-world effects."
            ),
        }
    )
    _write_json(output_directory.parent / "comparison.json", comparison)
    return comparison


def _advance_to(demo: DemoService, branch_id: str, target_minute: int) -> None:
    while demo.branches[branch_id].runtime.world.minute_index < target_minute:
        current = demo.branches[branch_id].runtime.world.minute_index
        demo.dispatch(
            "advance",
            {"branch_id": branch_id, "steps": min(ADVANCE_CHUNK, target_minute - current)},
        )


def _complete(demo: DemoService, *branch_ids: str) -> None:
    for branch_id in branch_ids:
        if demo.branches[branch_id].status == "paused":
            demo.dispatch("resume", {"branch_id": branch_id})
    for branch_id in branch_ids:
        while demo.branches[branch_id].runtime.world.minute_index < demo.scenario.duration_minutes:
            current = demo.branches[branch_id].runtime.world.minute_index
            demo.dispatch(
                "advance",
                {
                    "branch_id": branch_id,
                    "steps": min(ADVANCE_CHUNK, demo.scenario.duration_minutes - current),
                },
            )


def _write_branch_artifacts(
    scenario: ScenarioSpec,
    runtime: Any,
    output_directory: Path,
    *,
    branch_id: str,
    parent_branch_id: str | None,
    lineage: dict[str, Any],
) -> None:
    initial_world = WorldState.from_scenario(scenario)
    writer = RunArtifactWriter(scenario, output_directory, trace_level=TraceLevel.OFF)
    writer.write_initial(initial_world, execution_mode="precomputed_replay")
    writer.metrics = runtime.metrics
    writer.finalize(runtime.world)

    manifest_path = output_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "run_id": output_directory.name,
            "branch_id": branch_id,
            "parent_branch_id": parent_branch_id,
        }
    )
    _write_json(manifest_path, manifest)
    _write_json(output_directory / "branch_lineage.json", lineage)


def _world_hash(world: WorldState) -> str:
    return hashlib.sha256(world.canonical_json().encode("utf-8")).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "fixtures" / "demo_replay" / "runs",
        help="directory for the two saved run artifacts",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace existing output artifacts after confirming this full simulation is desired",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="refresh comparison.json from existing run artifacts without simulating",
    )
    arguments = parser.parse_args()
    output_directory = arguments.output.resolve()
    if arguments.report_only and arguments.force:
        parser.error("--report-only cannot be combined with --force")
    if arguments.report_only:
        comparison = _write_comparison(ScenarioSpec.load(SCENARIO_PATH), output_directory)
    elif output_directory.exists() and any(output_directory.iterdir()):
        if not arguments.force:
            parser.error("output directory is not empty; pass --force to replace the replay")
        shutil.rmtree(output_directory)
        (output_directory.parent / "comparison.json").unlink(missing_ok=True)
        comparison = build_replay(output_directory)
    else:
        comparison = build_replay(output_directory)
    print(
        json.dumps(
            {
                "execution_mode": comparison["execution_mode"],
                "agent_count": comparison["agent_count"],
                "duration_minutes": comparison["duration_minutes"],
                "fork_minute": comparison["fork_minute"],
                "first_divergence_minute": comparison["first_divergence_minute"],
                "baseline_run_id": comparison["baseline_run_id"],
                "candidate_run_id": comparison["candidate_run_id"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
