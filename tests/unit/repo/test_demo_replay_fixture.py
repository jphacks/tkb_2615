"""The packaged judge demo replay (fixtures/demo_replay) is a paired seven-day Branch.

Serving it over HTTP is checked in tests/integration/test_judge_demo.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from sheltersim.experiments.comparison import compare_run_artifacts

REPLAY_ROOT = Path(__file__).parents[3] / "fixtures" / "demo_replay"
RUNS = REPLAY_ROOT / "runs"
BASELINE = RUNS / "baseline"
CANDIDATE = RUNS / "day4-toilet-outage"


def test_packaged_day4_replay_is_a_paired_seven_day_branch() -> None:
    comparison = json.loads((REPLAY_ROOT / "comparison.json").read_text(encoding="utf-8"))
    baseline_manifest = json.loads((BASELINE / "manifest.json").read_text(encoding="utf-8"))
    candidate_manifest = json.loads((CANDIDATE / "manifest.json").read_text(encoding="utf-8"))
    lineage = json.loads((CANDIDATE / "branch_lineage.json").read_text(encoding="utf-8"))
    report = compare_run_artifacts(BASELINE, CANDIDATE)
    outage_events = [
        json.loads(line)
        for line in (CANDIDATE / "events_realized.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    outage_events = [
        item
        for item in outage_events
        if item["event"].get("provenance", {}).get("effect_program_v1", {}).get("op")
        == "facility_outage"
    ]

    assert comparison["execution_mode"] == "precomputed_replay"
    assert comparison["agent_count"] == 100
    assert comparison["duration_minutes"] == 10_080
    assert comparison["fork_minute"] == 5_160
    assert comparison["api_request_count"] == 0
    assert len(json.dumps(comparison)) < 20_000
    assert baseline_manifest["final_minute"] == candidate_manifest["final_minute"] == 10_080
    assert (
        baseline_manifest["execution_mode"]
        == candidate_manifest["execution_mode"]
        == "precomputed_replay"
    )
    assert candidate_manifest["parent_branch_id"] == "baseline"
    assert lineage["parent_branch_id"] == "baseline"
    assert lineage["fork_at_minute"] == 5_160
    assert report["seed"] == comparison["seed"]
    assert report["first_divergence_minute"] == comparison["first_divergence_minute"]
    assert report["first_divergence_minute"] is not None
    assert any(item["event"]["due_minute"] == 5_160 for item in outage_events)
    assert "single seed" in report["interpretation"]
