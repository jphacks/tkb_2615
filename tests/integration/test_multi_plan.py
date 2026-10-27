"""Multi-Plan alternatives validate and apply over HTTP as sibling Branches of one origin.

Refusals before anything is applied are checked in tests/unit/web/test_demo.py, and
comparing the applied siblings in tests/e2e/test_multi_plan.py.
"""

from __future__ import annotations

from pathlib import Path

from sheltersim.llm.schemas import (
    MultiPlanApproval,
    MultiPlanDraft,
)


def test_multi_plan_http_validate_and_apply_create_sibling_branches(
    tmp_path: Path, small_replay_demo, multi_plan_draft, post_demo_json
) -> None:
    demo = small_replay_demo(12, paused=True)
    source_hash = demo.branches["baseline"].runtime.world.canonical_json()
    before_branch_ids = set(demo.branches)
    plan = multi_plan_draft(demo)

    status, validation = post_demo_json(
        demo,
        tmp_path,
        "/api/v1/nl/multi-validate",
        {"source_branch_id": "baseline", "multi_plan": plan.model_dump(mode="json")},
    )
    assert status == 200
    assert validation["valid"] is True
    assert len(validation["alternatives"]) == 3
    assert validation["origin_checkpoint_id"] == "baseline-cp-0001"
    assert validation["approval_hash"]

    request = {
        "source_branch_id": "baseline",
        "approved_plan": {
            "multi_plan": plan.model_dump(mode="json"),
            "approval_hash": validation["approval_hash"],
            "approval_method": "explicit_user_confirmation",
        },
        "idempotency_key": "multi-preview-001",
    }
    apply_status, result = post_demo_json(demo, tmp_path, "/api/v1/nl/multi-apply", request)
    assert apply_status == 200
    assert len(result["branches"]) == 3
    assert set(demo.branches) == before_branch_ids | {
        item["branch_id"] for item in result["branches"]
    }
    assert demo.branches["baseline"].runtime.world.canonical_json() == source_hash
    assert {
        demo.branches[item["branch_id"]].runtime.parent_branch_id for item in result["branches"]
    } == {"baseline"}
    assert all(
        demo.branches[item["branch_id"]].runtime.runtime_state["multi_plan_v1"][
            "source_checkpoint_hash"
        ]
        == result["source_checkpoint_hash"]
        for item in result["branches"]
    )

    replay_status, replay = post_demo_json(demo, tmp_path, "/api/v1/nl/multi-apply", request)
    assert replay_status == 200
    assert replay["idempotent_replay"] is True
    assert [item["branch_id"] for item in replay["branches"]] == [
        item["branch_id"] for item in result["branches"]
    ]
    assert len(demo.branches) == 4

    changed = plan.model_dump(mode="json")
    changed["branches"][2]["plan"]["effects"][0]["start_minute"] = 1
    changed_plan = MultiPlanDraft.model_validate(changed)
    changed_receipt = MultiPlanApproval.from_explicit_confirmation(changed_plan)
    request["approved_plan"] = changed_receipt.model_dump(mode="json")
    reused_status, reused = post_demo_json(demo, tmp_path, "/api/v1/nl/multi-apply", request)
    assert reused_status == 400
    assert "different multi-plan" in reused["error"]
    assert len(demo.branches) == 4
