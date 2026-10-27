"""The demo's commands follow a strict schema and state order, and the packaged scenario
is the offline Day 3 fixture.

Multi-Plan alternatives are refused before anything is applied: an invalid one rejects
the whole group without changing the source, ambiguous, relative or multiple event times
need clarification, and repeated effects are rejected. Applying them over HTTP is
checked in tests/integration/test_multi_plan.py, and the demo's lifecycle in
tests/integration/test_web_demo.py.
"""

from __future__ import annotations

import pytest

from sheltersim.llm.schemas import (
    MultiPlanApproval,
    MultiPlanDraft,
)
from sheltersim.web.demo import DemoError, DemoService


def test_invalid_alternative_rejects_entire_group_without_source_or_branch_changes(
    paused_small_replay_demo, multi_plan_draft, apply_multi_plan
) -> None:
    demo = paused_small_replay_demo()
    plan_data = multi_plan_draft(demo).model_dump(mode="json")
    plan_data["branches"][1]["plan"]["effects"][0]["target_selector"]["target_id"] = "missing-zone"
    invalid = MultiPlanDraft.model_validate(plan_data)
    source_hash = demo.branches["baseline"].runtime.world.canonical_json()

    validation = demo.validate_multi_plan_preview(
        {"source_branch_id": "baseline", "multi_plan": invalid.model_dump(mode="json")}
    )
    assert validation["valid"] is False
    assert validation["alternatives"][1]["valid"] is False
    with pytest.raises(DemoError, match="multi-plan is not valid"):
        apply_multi_plan(demo, invalid, "invalid-multi-plan-001")
    assert set(demo.branches) == {"baseline"}
    assert demo.branches["baseline"].runtime.world.canonical_json() == source_hash
    assert demo.branches["baseline"].runtime.command_log == []


def test_unresolved_time_ambiguity_cannot_be_validated_or_approved(
    paused_small_replay_demo, multi_plan_draft
) -> None:
    demo = paused_small_replay_demo()
    plan_data = multi_plan_draft(demo, variants=2).model_dump(mode="json")
    for alternative in plan_data["branches"]:
        alternative["plan"]["support_mode"] = "clarification_required"
        alternative["plan"]["ambiguities"] = [
            {
                "code": "ambiguous_time",
                "field": "effects.start_minute",
                "message": "翌朝が何時か確認してください。",
                "choices": ["HH:MM"],
            }
        ]
    ambiguous = MultiPlanDraft.model_validate(plan_data)
    validation = demo.validate_multi_plan_preview(
        {"source_branch_id": "baseline", "multi_plan": ambiguous.model_dump(mode="json")}
    )
    assert validation["valid"] is False
    assert any("unresolved time" in item for item in validation["issues"])
    with pytest.raises(ValueError, match="unresolved ambiguities"):
        MultiPlanApproval.from_explicit_confirmation(ambiguous)


def test_relative_and_multiple_event_times_require_clarification(paused_small_replay_demo) -> None:
    from sheltersim.llm.natural_language import (
        compile_natural_language,
        scenario_summary_from_world,
    )
    from sheltersim.llm.provider import LLMMode, LLMRouter, LLMSettings
    from sheltersim.llm.schemas import ParseRequest

    demo = paused_small_replay_demo()
    world = demo.branches["baseline"].runtime.world
    scenario_summary = scenario_summary_from_world(world)
    router = LLMRouter(LLMSettings(mode=LLMMode.OFFLINE))
    for text in (
        "Day4 water outage, next morning add 2 toilets",
        "Day4 14:00 water outage and Day5 08:00 add 2 toilets",
        "Day4 noon toilet outage",
    ):
        result = compile_natural_language(
            ParseRequest(
                text=text,
                branch_id="baseline",
                selected_minute=0,
                scenario_summary=scenario_summary,
            ),
            router,
        )
        assert result.status == "clarification_required"
        assert result.draft_plan is None
        assert any("時刻" in question for question in result.questions)


def test_duplicate_effects_are_rejected_even_when_effect_ids_differ(
    paused_small_replay_demo, multi_plan_draft
) -> None:
    demo = paused_small_replay_demo()
    data = multi_plan_draft(demo, variants=2).model_dump(mode="json")
    duplicate_effect = dict(data["branches"][0]["plan"]["effects"][0])
    duplicate_effect["effect_id"] = "different-display-id"
    data["branches"][1]["plan"]["effects"] = [duplicate_effect]
    data["branches"][1]["plan"]["program_id"] = "different-program-id"
    plan = MultiPlanDraft.model_validate(data)

    validation = demo.validate_multi_plan_preview(
        {"source_branch_id": "baseline", "multi_plan": plan.model_dump(mode="json")}
    )
    assert validation["valid"] is False
    assert "same modeled effects" in validation["alternatives"][1]["issues"][0]
    with pytest.raises(ValueError, match="different modeled effects"):
        MultiPlanApproval.from_explicit_confirmation(plan)


@pytest.fixture
def demo(day3_demo: DemoService) -> DemoService:
    return day3_demo


def test_demo_command_schema_and_state_transitions_are_strict(demo: DemoService) -> None:
    with pytest.raises(DemoError, match="start the demo"):
        demo.dispatch("pause", {"branch_id": "baseline"})
    demo.dispatch("start")
    with pytest.raises(DemoError, match="unsupported"):
        demo.dispatch("advance", {"branch_id": "baseline", "steps": 1, "unexpected": True})
    with pytest.raises(DemoError, match="integer"):
        demo.dispatch("advance", {"branch_id": "baseline", "steps": True})
    with pytest.raises(DemoError, match="must be paused"):
        demo.dispatch("command", {"branch_id": "baseline", "command": {}})


def test_packaged_demo_scenario_is_the_offline_day3_fixture() -> None:
    from sheltersim.web.demo import load_demo_scenario

    scenario = load_demo_scenario()
    assert scenario.id == "failure_day3"
    assert len(scenario.profiles) == 100
    assert {event.due_minute for event in scenario.scheduled_events} == {2880}
