"""The offline browser demo (web/demo.py): DemoService setups, plans and its HTTP routes."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from support.paths import FAILURE_DAY3, SMALL_REPLAY


def _small_replay_demo(duration_minutes: int = 5200) -> Any:
    from typing import get_args

    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.web.demo import DemoService

    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    # The ScenarioSpec schema evolves independently from this compact regression fixture.
    document["schema_version"] = get_args(ScenarioSpec.model_fields["schema_version"].annotation)[0]
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = []
    return DemoService(ScenarioSpec.model_validate(document))


@pytest.fixture
def small_replay_demo() -> Callable[..., Any]:
    """Build a DemoService over h00_small_replay without events, 5200 minutes long by default."""
    return _small_replay_demo


# Day 4, 14:00: where the timed-plan tests fork.
DAY4_1400 = 3 * 1440 + 14 * 60


def _day4_timed_action(
    *,
    effect_id: str,
    op: str,
    target_kind: str,
    target_id: str,
    parameters: dict[str, Any],
    units: str,
    policy_dependencies: list[str],
    start_minute: int = DAY4_1400,
    end_minute: int | None = None,
    guard: Any = None,
    reversible: bool = True,
) -> Any:
    from sheltersim.events.effects import TargetSelector, TimedAction

    return TimedAction(
        effect_id=effect_id,
        op=op,
        target_selector=TargetSelector(kind=target_kind, target_id=target_id),
        start_minute=start_minute,
        end_minute=end_minute,
        guard=guard,
        parameters=parameters,
        units=units,
        source_id="H08-TEST-PLAN",
        assumption_ids=[],
        policy_dependencies=policy_dependencies,
        reversible=reversible,
    )


def _day4_plan(*, narrative: str = "Approved Day 4 plan", fork_minute: int = DAY4_1400) -> Any:
    from sheltersim.events.effects import GuardExpression, SupportMode
    from sheltersim.llm.schemas import DraftPlan

    actions = [
        _day4_timed_action(
            effect_id="temporary-toilet-outage",
            op="set_facility_availability",
            target_kind="facility",
            target_id="toilet_1",
            parameters={"working_units": 0},
            units="facility_unit",
            policy_dependencies=["facility_capacity"],
            start_minute=fork_minute,
            end_minute=fork_minute + 2,
            guard=GuardExpression(operator="gte", field="target.working_units", value=1),
        ),
        _day4_timed_action(
            effect_id="add-temporary-toilet",
            op="add_toilet",
            target_kind="zone",
            target_id="toilet",
            parameters={"count": 1},
            units="toilet_stall",
            policy_dependencies=["facility_capacity"],
            start_minute=fork_minute,
            reversible=False,
        ),
        _day4_timed_action(
            effect_id="add-support-staff",
            op="add_staff",
            target_kind="zone",
            target_id="information_desk",
            parameters={"count": 1},
            units="staff_member",
            policy_dependencies=["staff_operations"],
            start_minute=fork_minute,
            reversible=False,
        ),
    ]
    return DraftPlan(
        program_id="h08-approved-plan",
        narrative=narrative,
        source_text="Synthetic test preview; add one toilet and staff member.",
        simulation_start_minute=fork_minute,
        effects=actions,
        assumptions=[],
        support_mode=SupportMode.COMPOSED,
        compiler_provenance={
            "compiler_kind": "offline_rule",
            "compiler_version": "h08-test-v1",
        },
        branch_id="baseline",
        fork_at_minute=fork_minute,
        ambiguities=[],
        requires_confirmation=True,
    )


@pytest.fixture
def day4_plan() -> Callable[..., Any]:
    """Build a DraftPlan for the baseline Branch: a two-minute toilet outage, one added
    toilet and one added support worker, forking at Day 4 14:00 by default.
    """
    return _day4_plan


def _failure_day3_demo(duration: int = 12, *, start: bool = True) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.web.demo import DemoService

    document = json.loads(FAILURE_DAY3.read_text(encoding="utf-8"))
    document["duration_minutes"] = duration
    document["scheduled_events"] = []
    demo = DemoService(ScenarioSpec.model_validate(document))
    if start:
        demo.dispatch("start")
    return demo


@pytest.fixture
def failure_day3_demo() -> Callable[..., Any]:
    """Build a DemoService over failure_day3 without its events, 12 minutes long and
    started by default.
    """
    return _failure_day3_demo


# Only stops a hung worker from hanging the run; a finished job never waits this long.
JOB_HANG_SECONDS = 120


def _wait_for_job(manager: Any, job_id: str, expected: set[str]) -> dict:
    # The manager runs jobs one at a time on a single worker, so a no-op queued now
    # finishes only after every job submitted before it (#143: a 5-second polling
    # deadline depended on how busy the machine was).
    manager._executor.submit(lambda: None).result(timeout=JOB_HANG_SECONDS)
    job = manager.get(job_id)
    if job["status"] not in expected:
        pytest.fail(f"job {job_id} finished as {job['status']}, not {sorted(expected)}: {job}")
    return job


@pytest.fixture
def wait_for_job() -> Callable[[Any, str, set[str]], dict]:
    """Wait for the demo jobs submitted so far to finish, then require one of the statuses."""
    return _wait_for_job


def _paused_small_replay_demo(duration_minutes: int = 12) -> Any:
    from typing import get_args

    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.web.demo import DemoService

    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["schema_version"] = get_args(ScenarioSpec.model_fields["schema_version"].annotation)[0]
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = []
    demo = DemoService(ScenarioSpec.model_validate(document))
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    return demo


@pytest.fixture
def paused_small_replay_demo() -> Callable[..., Any]:
    """Build a DemoService over h00_small_replay without events, 12 minutes long by
    default, started and paused on the baseline Branch.
    """
    return _paused_small_replay_demo


MULTI_PLAN_PROVENANCE = {"compiler_kind": "offline_rule", "compiler_version": "a04-multi-test-v1"}


def _multi_plan_effect(variant: int, *, start_minute: int = 0) -> Any:
    from sheltersim.events.effects import TargetSelector, TimedAction

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


def _multi_plan_alternative(variant: int, *, start_minute: int = 0) -> Any:
    from sheltersim.events.effects import SupportMode
    from sheltersim.llm.schemas import DraftPlan

    return DraftPlan(
        program_id=f"a04-plan-{variant}",
        narrative=f"Alternative {variant}",
        source_text="Synthetic typed alternative",
        simulation_start_minute=0,
        effects=[_multi_plan_effect(variant, start_minute=start_minute)],
        assumptions=[],
        support_mode=SupportMode.COMPOSED,
        compiler_provenance=MULTI_PLAN_PROVENANCE,
        branch_id="baseline",
        fork_at_minute=0,
        ambiguities=[],
        requires_confirmation=True,
    )


def _multi_plan_draft(demo: Any, *, variants: int = 3) -> Any:
    import hashlib

    from sheltersim.llm.schemas import MultiPlanDraft

    state_hash = hashlib.sha256(
        demo.branches["baseline"].runtime.world.canonical_json().encode("utf-8")
    ).hexdigest()
    return MultiPlanDraft(
        schema_version=1,
        branch_origin="baseline",
        source_state_hash=state_hash,
        fork_at_minute=0,
        parser_provenance=MULTI_PLAN_PROVENANCE,
        branches=[
            {
                "variant_id": f"alternative-{variant}",
                "label": f"比較案 {variant}",
                "plan": _multi_plan_alternative(variant),
            }
            for variant in range(1, variants + 1)
        ],
    )


@pytest.fixture
def multi_plan_draft() -> Callable[..., Any]:
    """Build a MultiPlanDraft from the demo's baseline: up to three alternatives that add
    a worker, add a toilet, or close toilet_1 for two minutes.
    """
    return _multi_plan_draft


def _post_demo_json(
    demo: Any,
    tmp_path: Path,
    route: str,
    document: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    from io import BytesIO

    from sheltersim.web.inspector import InspectorService
    from sheltersim.web.server import _handler_for

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


@pytest.fixture
def post_demo_json() -> Callable[..., tuple[int, dict[str, Any]]]:
    """POST a JSON document to a demo HTTP route; return the status and the response."""
    return _post_demo_json


def _apply_multi_plan(demo: Any, plan: Any, key: str) -> dict[str, Any]:
    from sheltersim.llm.schemas import MultiPlanApproval

    approval = MultiPlanApproval.from_explicit_confirmation(plan)
    return demo.apply_multi_plan_preview(
        {
            "source_branch_id": plan.branch_origin,
            "approved_plan": approval.model_dump(mode="json"),
            "idempotency_key": key,
        }
    )


@pytest.fixture
def apply_multi_plan() -> Callable[[Any, Any, str], dict[str, Any]]:
    """Approve a MultiPlanDraft explicitly and apply it to the demo."""
    return _apply_multi_plan


@pytest.fixture
def day3_demo() -> Any:
    """A DemoService over failure_day3, eight minutes long, with its events due at minute 2."""
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.web.demo import DemoService

    document = json.loads(FAILURE_DAY3.read_text(encoding="utf-8"))
    document["duration_minutes"] = 8
    document["scheduled_events"] = [
        {**event, "due_minute": 2} for event in document["scheduled_events"]
    ]
    return DemoService(ScenarioSpec.model_validate(document))


def _paused_offline_compiler_demo(duration_minutes: int = 20) -> Any:
    from typing import get_args

    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.llm.provider import LLMMode, LLMRouter, LLMSettings
    from sheltersim.web.demo import DemoService

    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["schema_version"] = get_args(ScenarioSpec.model_fields["schema_version"].annotation)[0]
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = []
    demo = DemoService(ScenarioSpec.model_validate(document))
    demo.compiler_router = LLMRouter(LLMSettings(mode=LLMMode.OFFLINE))
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    return demo


@pytest.fixture
def paused_offline_compiler_demo() -> Callable[..., Any]:
    """Build a DemoService over h00_small_replay without events, 20 minutes long by default,
    with an offline Event Compiler, started and paused on the baseline Branch.
    """
    return _paused_offline_compiler_demo


def _nl_interpret_preview(demo: Any, tmp_path: Path, selected_minute: int = 5) -> dict[str, Any]:
    status, response = _post_demo_json(
        demo,
        tmp_path,
        "/api/v1/nl/interpret",
        {
            "text": "toilet_1 outage",
            "branch_id": "baseline",
            "selected_minute": selected_minute,
        },
    )
    assert status == 200
    assert response["result"]["status"] == "compiled"
    assert response["result"]["fallback_used"] is True
    assert response["validation"]["valid"] is True
    return response


@pytest.fixture
def nl_interpret_preview() -> Callable[..., dict[str, Any]]:
    """Post "toilet_1 outage" to the natural-language interpret route for the baseline and
    check that the offline rule compiled a valid plan.
    """
    return _nl_interpret_preview
