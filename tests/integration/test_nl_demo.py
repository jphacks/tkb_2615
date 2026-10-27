"""The natural-language demo preview over HTTP changes nothing, needs a paused source, and
explains inputs it cannot use instead of making a rule plan.

Validating, approving and applying a preview as a Branch is checked in
tests/e2e/test_nl_demo_branching.py.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from sheltersim.llm.provider import LLMMode, LLMRouter, LLMSettings
from sheltersim.web.demo import DemoService


def _world_hash(demo: DemoService, branch_id: str = "baseline") -> str:
    world = demo.branches[branch_id].runtime.world
    return hashlib.sha256(world.canonical_json().encode("utf-8")).hexdigest()


def test_interpret_preview_does_not_change_world_branch_or_event_queue(
    tmp_path: Path, paused_offline_compiler_demo, nl_interpret_preview
) -> None:
    demo = paused_offline_compiler_demo()
    source_hash = _world_hash(demo)
    source_events = [
        event.event_id for event in demo.branches["baseline"].runtime.world.event_queue.pending()
    ]

    preview = nl_interpret_preview(demo, tmp_path)

    assert preview["preview_id"]
    assert preview["result"]["draft_plan"]["requires_confirmation"] is True
    assert _world_hash(demo) == source_hash
    assert set(demo.branches) == {"baseline"}
    assert [
        event.event_id for event in demo.branches["baseline"].runtime.world.event_queue.pending()
    ] == source_events
    assert demo.natural_language_audit[-1]["source_state_hash"] == source_hash
    assert demo.natural_language_audit[-1]["effects"]


def test_interpret_requires_a_paused_source_branch(
    tmp_path: Path, paused_offline_compiler_demo, post_demo_json
) -> None:
    demo = paused_offline_compiler_demo()
    demo.dispatch("resume", {"branch_id": "baseline"})

    status, response = post_demo_json(
        demo,
        tmp_path,
        "/api/v1/nl/interpret",
        {"text": "toilet_1 outage", "branch_id": "baseline", "selected_minute": 5},
    )

    assert status == 409
    assert "pause" in response["error"]
    assert not demo.natural_language_previews
    assert not demo.natural_language_audit


def test_unsupported_offline_input_is_explained_and_cannot_be_approved(
    tmp_path: Path, paused_offline_compiler_demo, post_demo_json
) -> None:
    demo = paused_offline_compiler_demo()
    status, preview = post_demo_json(
        demo,
        tmp_path,
        "/api/v1/nl/interpret",
        {"text": "子どもが泣き続け避難者が眠れない", "branch_id": "baseline", "selected_minute": 0},
    )
    assert status == 200
    assert preview["result"]["status"] == "not_simulatable"
    assert preview["result"]["limitations"]
    assert preview["result"]["draft_plan"] is None
    assert preview["validation"]["valid"] is False

    status, result = post_demo_json(
        demo,
        tmp_path,
        "/api/v1/nl/apply",
        {
            "source_branch_id": "baseline",
            "preview_id": preview["preview_id"],
            "idempotency_key": "unsupported-preview-001",
        },
    )
    assert status == 400
    assert "compiled" in result["error"]
    assert set(demo.branches) == {"baseline"}


def test_interpret_under_stop_policy_returns_the_reason_instead_of_a_rule_plan(
    tmp_path: Path, paused_offline_compiler_demo, post_demo_json
) -> None:
    from sheltersim.llm.config import LLMErrorPolicy

    demo = paused_offline_compiler_demo()
    source_hash = _world_hash(demo)
    # A stop policy without a key cannot record; replay has no tape, so nothing is called.
    demo._compiler_routers["baseline"] = LLMRouter(
        LLMSettings(mode=LLMMode.REPLAY, error_policy=LLMErrorPolicy.STOP)
    )

    status, response = post_demo_json(
        demo,
        tmp_path,
        "/api/v1/nl/interpret",
        {"text": "toilet_1 outage", "branch_id": "baseline", "selected_minute": 5},
    )

    assert status == 409
    assert "記録済みの応答がない" in response["error"]
    assert "replay_miss" in response["error"]
    assert demo.natural_language_previews == {}
    assert _world_hash(demo) == source_hash
