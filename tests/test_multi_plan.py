"""Multi-Plan alternatives validate and apply atomically from a shared Branch origin."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Any, get_args

import pytest

from sheltersim.events.effects import (
    SupportMode,
    TargetSelector,
    TimedAction,
)
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.schemas import (
    DraftPlan,
    MultiPlanApproval,
    MultiPlanDraft,
)
from sheltersim.web.demo import DemoError, DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenarios" / "h00_small_replay.json"
_PROVENANCE = {"compiler_kind": "offline_rule", "compiler_version": "a04-multi-test-v1"}


def _demo(duration_minutes: int = 12) -> DemoService:
    document = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    document["schema_version"] = get_args(ScenarioSpec.model_fields["schema_version"].annotation)[0]
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = []
    demo = DemoService(ScenarioSpec.model_validate(document))
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    return demo


def _effect(variant: int, *, start_minute: int = 0) -> TimedAction:
    if variant == 1:
        return TimedAction(
            effect_id="effect-add-staff",
            op="add_staff",
            target_selector=TargetSelector(kind="zone", target_id="information_desk"),
            start_minute=start_minute,
            parameters={"count": 1},
            units="staff_member",
            source_id="A04-MULTI-TEST",
            policy_dependencies=["staff_operations"],
            reversible=False,
        )
    if variant == 2:
        return TimedAction(
            effect_id="effect-add-toilet",
            op="add_toilet",
            target_selector=TargetSelector(kind="zone", target_id="toilet"),
            start_minute=start_minute,
            parameters={"count": 1},
            units="toilet_stall",
            source_id="A04-MULTI-TEST",
            policy_dependencies=["facility_capacity"],
            reversible=False,
        )
    return TimedAction(
        effect_id="effect-close-toilet",
        op="set_facility_availability",
        target_selector=TargetSelector(kind="facility", target_id="toilet_1"),
        start_minute=start_minute,
        end_minute=start_minute + 2,
        parameters={"working_units": 0},
        units="facility_unit",
        source_id="A04-MULTI-TEST",
        policy_dependencies=["facility_capacity"],
        reversible=True,
    )


def _draft(variant: int, *, start_minute: int = 0) -> DraftPlan:
    return DraftPlan(
        program_id=f"a04-plan-{variant}",
        narrative=f"Alternative {variant}",
        source_text="Synthetic typed alternative",
        simulation_start_minute=0,
        effects=[_effect(variant, start_minute=start_minute)],
        assumptions=[],
        support_mode=SupportMode.COMPOSED,
        compiler_provenance=_PROVENANCE,
        branch_id="baseline",
        fork_at_minute=0,
        ambiguities=[],
        requires_confirmation=True,
    )


def _multi_plan(demo: DemoService, *, variants: int = 3) -> MultiPlanDraft:
    state_hash = hashlib.sha256(
        demo.branches["baseline"].runtime.world.canonical_json().encode("utf-8")
    ).hexdigest()
    return MultiPlanDraft(
        schema_version=1,
        branch_origin="baseline",
        source_state_hash=state_hash,
        fork_at_minute=0,
        parser_provenance=_PROVENANCE,
        branches=[
            {
                "variant_id": f"alternative-{variant}",
                "label": f"比較案 {variant}",
                "plan": _draft(variant),
            }
            for variant in range(1, variants + 1)
        ],
    )


def _post(
    demo: DemoService,
    tmp_path: Path,
    route: str,
    document: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    raw = json.dumps(document).encode("utf-8")
    handler_type = _handler_for(InspectorService(tmp_path), demo)
    handler = handler_type.__new__(handler_type)
    handler.path = route
    handler.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json"}
    handler.rfile = BytesIO(raw)
    responses: list[tuple[int, dict[str, Any]]] = []
    handler._json_response = lambda status, value: responses.append((status, value))
    handler.do_POST()
    return responses[0]


def _apply(demo: DemoService, plan: MultiPlanDraft, key: str) -> dict[str, Any]:
    approval = MultiPlanApproval.from_explicit_confirmation(plan)
    return demo.apply_multi_plan_preview(
        {
            "source_branch_id": plan.branch_origin,
            "approved_plan": approval.model_dump(mode="json"),
            "idempotency_key": key,
        }
    )


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


def test_multi_plan_http_validate_and_apply_create_sibling_branches(tmp_path: Path) -> None:
    demo = _demo()
    source_hash = demo.branches["baseline"].runtime.world.canonical_json()
    before_branch_ids = set(demo.branches)
    plan = _multi_plan(demo)

    status, validation = _post(
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
    apply_status, result = _post(demo, tmp_path, "/api/v1/nl/multi-apply", request)
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

    replay_status, replay = _post(demo, tmp_path, "/api/v1/nl/multi-apply", request)
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
    reused_status, reused = _post(demo, tmp_path, "/api/v1/nl/multi-apply", request)
    assert reused_status == 400
    assert "different multi-plan" in reused["error"]
    assert len(demo.branches) == 4


def test_invalid_alternative_rejects_entire_group_without_source_or_branch_changes() -> None:
    demo = _demo()
    plan_data = _multi_plan(demo).model_dump(mode="json")
    plan_data["branches"][1]["plan"]["effects"][0]["target_selector"]["target_id"] = "missing-zone"
    invalid = MultiPlanDraft.model_validate(plan_data)
    source_hash = demo.branches["baseline"].runtime.world.canonical_json()

    validation = demo.validate_multi_plan_preview(
        {"source_branch_id": "baseline", "multi_plan": invalid.model_dump(mode="json")}
    )
    assert validation["valid"] is False
    assert validation["alternatives"][1]["valid"] is False
    with pytest.raises(DemoError, match="multi-plan is not valid"):
        _apply(demo, invalid, "invalid-multi-plan-001")
    assert set(demo.branches) == {"baseline"}
    assert demo.branches["baseline"].runtime.world.canonical_json() == source_hash
    assert demo.branches["baseline"].runtime.command_log == []


def test_unresolved_time_ambiguity_cannot_be_validated_or_approved() -> None:
    demo = _demo()
    plan_data = _multi_plan(demo, variants=2).model_dump(mode="json")
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


def test_three_branch_set_compares_only_compatible_siblings(tmp_path: Path) -> None:
    demo = _demo()
    result = _apply(demo, _multi_plan(demo), "multi-compare-001")
    branch_ids = [item["branch_id"] for item in result["branches"]]
    for branch_id in ["baseline", *branch_ids]:
        _complete(demo, branch_id)

    comparison_status, comparison = _post(
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
    mismatch_status, mismatch = _post(
        demo,
        tmp_path,
        "/api/v1/demo/compare-set",
        {"baseline_branch_id": "baseline", "candidate_branch_ids": branch_ids[:2]},
    )
    assert mismatch_status == 400
    assert "do not share one plan origin/checkpoint/model" in mismatch["error"]


def test_multi_plan_apply_rejects_stale_source_after_validation(tmp_path: Path) -> None:
    demo = _demo()
    plan = _multi_plan(demo, variants=2)
    validation = demo.validate_multi_plan_preview(
        {"source_branch_id": "baseline", "multi_plan": plan.model_dump(mode="json")}
    )
    assert validation["valid"]
    approved = MultiPlanApproval.from_explicit_confirmation(plan)
    demo.dispatch("resume", {"branch_id": "baseline"})
    demo.dispatch("advance", {"branch_id": "baseline", "steps": 1})
    demo.dispatch("pause", {"branch_id": "baseline"})

    status, response = _post(
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


def test_relative_and_multiple_event_times_require_clarification() -> None:
    from sheltersim.llm.natural_language import (
        compile_natural_language,
        scenario_summary_from_world,
    )
    from sheltersim.llm.provider import LLMMode, LLMRouter, LLMSettings
    from sheltersim.llm.schemas import ParseRequest

    demo = _demo()
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


def test_duplicate_effects_are_rejected_even_when_effect_ids_differ() -> None:
    demo = _demo()
    data = _multi_plan(demo, variants=2).model_dump(mode="json")
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
