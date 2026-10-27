"""Multi-Plan alternatives applied over HTTP run as sibling Branches: only compatible
siblings are compared, and a source that changed after validation is refused.
"""

from __future__ import annotations

from pathlib import Path

from sheltersim.llm.schemas import MultiPlanApproval
from sheltersim.web.demo import DemoService


def _complete(demo: DemoService, branch_id: str) -> None:
    branch = demo.branches[branch_id]
    if branch.status == "completed":
        return
    if branch.status == "paused":
        demo.dispatch("resume", {"branch_id": branch_id})
    remaining = branch.runtime.world.duration_minutes - branch.runtime.world.minute_index
    while remaining:
        steps = min(3000, remaining)
        demo.dispatch("advance", {"branch_id": branch_id, "steps": steps})
        remaining -= steps
        branch = demo.branches[branch_id]
        if remaining and branch.status == "paused":
            demo.dispatch("resume", {"branch_id": branch_id})


def test_three_branch_set_compares_only_compatible_siblings(
    tmp_path: Path, small_replay_demo, multi_plan_draft, post_demo_json, apply_multi_plan
) -> None:
    demo = small_replay_demo(12, paused=True)
    result = apply_multi_plan(demo, multi_plan_draft(demo), "multi-compare-001")
    branch_ids = [item["branch_id"] for item in result["branches"]]
    for branch_id in ["baseline", *branch_ids]:
        _complete(demo, branch_id)

    comparison_status, comparison = post_demo_json(
        demo,
        tmp_path,
        "/api/v1/demo/compare-set",
        {"baseline_branch_id": "baseline", "candidate_branch_ids": branch_ids[:2]},
    )
    assert comparison_status == 200
    assert comparison["compatibility"]["matched"] is True
    assert comparison["compatibility"]["seed"] == demo.scenario.seed
    assert len(comparison["comparisons"]) == 2

    one_candidate = demo.branches[branch_ids[0]]
    one_candidate.runtime.runtime_state["multi_plan_v1"]["model_versions_hash"] = "0" * 64
    mismatch_status, mismatch = post_demo_json(
        demo,
        tmp_path,
        "/api/v1/demo/compare-set",
        {"baseline_branch_id": "baseline", "candidate_branch_ids": branch_ids[:2]},
    )
    assert mismatch_status == 400
    assert "do not share one plan origin/checkpoint/model" in mismatch["error"]


def test_multi_plan_apply_rejects_stale_source_after_validation(
    tmp_path: Path, small_replay_demo, multi_plan_draft, post_demo_json
) -> None:
    demo = small_replay_demo(12, paused=True)
    plan = multi_plan_draft(demo, variants=2)
    validation = demo.validate_multi_plan_preview(
        {"source_branch_id": "baseline", "multi_plan": plan.model_dump(mode="json")}
    )
    assert validation["valid"]
    approved = MultiPlanApproval.from_explicit_confirmation(plan)
    demo.dispatch("resume", {"branch_id": "baseline"})
    demo.dispatch("advance", {"branch_id": "baseline", "steps": 1})
    demo.dispatch("pause", {"branch_id": "baseline"})

    status, response = post_demo_json(
        demo,
        tmp_path,
        "/api/v1/nl/multi-apply",
        {
            "source_branch_id": "baseline",
            "approved_plan": approved.model_dump(mode="json"),
            "idempotency_key": "multi-stale-001",
        },
    )
    assert status == 409
    assert "source Branch changed" in response["error"]
    assert set(demo.branches) == {"baseline"}
