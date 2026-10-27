"""A natural-language demo preview is validated, approved and applied over HTTP as a Branch:
retries are idempotent, a completed source can fork in the past, and a preview of a
source that changed since cannot be applied.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from sheltersim.web.demo import DemoService


def _world_hash(demo: DemoService, branch_id: str = "baseline") -> str:
    world = demo.branches[branch_id].runtime.world
    return hashlib.sha256(world.canonical_json().encode("utf-8")).hexdigest()


def test_validation_apply_and_idempotent_retry_preserve_source_branch(
    tmp_path: Path, paused_offline_compiler_demo, post_demo_json, nl_interpret_preview
) -> None:
    demo = paused_offline_compiler_demo()
    source_hash = _world_hash(demo)
    preview = nl_interpret_preview(demo, tmp_path)

    status, validation = post_demo_json(
        demo,
        tmp_path,
        "/api/v1/nl/validate",
        {"branch_id": "baseline", "preview_id": preview["preview_id"]},
    )
    assert status == 200
    assert validation["valid"] is True

    request = {
        "source_branch_id": "baseline",
        "preview_id": preview["preview_id"],
        "idempotency_key": "judge-preview-001",
    }
    status, result = post_demo_json(demo, tmp_path, "/api/v1/nl/apply", request)
    assert status == 200
    assert result["idempotent_replay"] is False
    child_id = result["branch_id"]
    assert child_id in demo.branches
    assert _world_hash(demo) == source_hash
    assert demo.branches[child_id].runtime.world.event_queue.pending()

    demo.dispatch("resume", {"branch_id": "baseline"})
    demo.dispatch("advance", {"branch_id": "baseline", "steps": 1})
    demo.dispatch("pause", {"branch_id": "baseline"})
    changed_source_hash = _world_hash(demo)
    assert changed_source_hash != source_hash

    repeated_status, repeated = post_demo_json(demo, tmp_path, "/api/v1/nl/apply", request)
    assert repeated_status == 200
    assert repeated["branch_id"] == child_id
    assert repeated["idempotent_replay"] is True
    assert _world_hash(demo) == changed_source_hash

    branch_summary = next(
        item for item in demo.state()["branches"] if item["branch_id"] == child_id
    )
    approved_plan = branch_summary["approved_plan"]
    assert "source_text" not in approved_plan
    assert approved_plan["source_text_hash"] == hashlib.sha256(b"toilet_1 outage").hexdigest()
    assert demo.natural_language_audit[-1]["source_text"] == "toilet_1 outage"
    assert approved_plan["compiler_provenance"]["compiler_kind"] == "offline_rule"
    assert approved_plan["effects"]
    assert demo.natural_language_audit[-1]["approved_branch_id"] == child_id


def test_completed_source_can_create_a_past_fork_without_changing_it(
    tmp_path: Path, paused_offline_compiler_demo, post_demo_json, nl_interpret_preview
) -> None:
    demo = paused_offline_compiler_demo(duration_minutes=20)
    demo.dispatch("resume", {"branch_id": "baseline"})
    demo.dispatch("advance", {"branch_id": "baseline", "steps": 20})
    source_hash = _world_hash(demo)
    preview = nl_interpret_preview(demo, tmp_path, selected_minute=9)

    status, result = post_demo_json(
        demo,
        tmp_path,
        "/api/v1/nl/apply",
        {
            "source_branch_id": "baseline",
            "preview_id": preview["preview_id"],
            "idempotency_key": "completed-source-001",
        },
    )

    assert status == 200
    assert result["branch_id"] in demo.branches
    assert demo.branches[result["branch_id"]].runtime.world.minute_index == 20
    assert _world_hash(demo) == source_hash


def test_preview_is_stale_after_source_branch_changes_and_cannot_be_applied(
    tmp_path: Path, paused_offline_compiler_demo, post_demo_json, nl_interpret_preview
) -> None:
    demo = paused_offline_compiler_demo()
    preview = nl_interpret_preview(demo, tmp_path)
    demo.dispatch("resume", {"branch_id": "baseline"})
    demo.dispatch("advance", {"branch_id": "baseline", "steps": 1})
    demo.dispatch("pause", {"branch_id": "baseline"})
    changed_hash = _world_hash(demo)

    status, validation = post_demo_json(
        demo,
        tmp_path,
        "/api/v1/nl/validate",
        {"branch_id": "baseline", "preview_id": preview["preview_id"]},
    )
    assert status == 200
    assert validation["valid"] is False
    assert validation["stale"] is True

    status, result = post_demo_json(
        demo,
        tmp_path,
        "/api/v1/nl/apply",
        {
            "source_branch_id": "baseline",
            "preview_id": preview["preview_id"],
            "idempotency_key": "stale-preview-001",
        },
    )
    assert status == 409
    assert "changed" in result["error"]
    assert set(demo.branches) == {"baseline"}
    assert _world_hash(demo) == changed_hash
