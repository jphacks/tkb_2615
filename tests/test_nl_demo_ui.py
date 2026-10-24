"""Natural-language demo preview, validation, and explicit branch approval routes."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Any, get_args

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.provider import LLMMode, LLMRouter, LLMSettings
from sheltersim.web.demo import DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenarios" / "h00_small_replay.json"


def _demo(duration_minutes: int = 20) -> DemoService:
    document = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    document["schema_version"] = get_args(ScenarioSpec.model_fields["schema_version"].annotation)[0]
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = []
    demo = DemoService(ScenarioSpec.model_validate(document))
    demo.compiler_router = LLMRouter(LLMSettings(mode=LLMMode.OFFLINE))
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    return demo


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


def _preview(demo: DemoService, tmp_path: Path, selected_minute: int = 5) -> dict[str, Any]:
    status, response = _post(
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


def _world_hash(demo: DemoService, branch_id: str = "baseline") -> str:
    world = demo.branches[branch_id].runtime.world
    return hashlib.sha256(world.canonical_json().encode("utf-8")).hexdigest()


def test_interpret_preview_does_not_change_world_branch_or_event_queue(tmp_path: Path) -> None:
    demo = _demo()
    source_hash = _world_hash(demo)
    source_events = [
        event.event_id for event in demo.branches["baseline"].runtime.world.event_queue.pending()
    ]

    preview = _preview(demo, tmp_path)

    assert preview["preview_id"]
    assert preview["result"]["draft_plan"]["requires_confirmation"] is True
    assert _world_hash(demo) == source_hash
    assert set(demo.branches) == {"baseline"}
    assert [
        event.event_id for event in demo.branches["baseline"].runtime.world.event_queue.pending()
    ] == source_events
    assert demo.natural_language_audit[-1]["source_state_hash"] == source_hash
    assert demo.natural_language_audit[-1]["effects"]


def test_interpret_requires_a_paused_source_branch(tmp_path: Path) -> None:
    demo = _demo()
    demo.dispatch("resume", {"branch_id": "baseline"})

    status, response = _post(
        demo,
        tmp_path,
        "/api/v1/nl/interpret",
        {"text": "toilet_1 outage", "branch_id": "baseline", "selected_minute": 5},
    )

    assert status == 409
    assert "pause" in response["error"]
    assert not demo.natural_language_previews
    assert not demo.natural_language_audit


def test_validation_apply_and_idempotent_retry_preserve_source_branch(tmp_path: Path) -> None:
    demo = _demo()
    source_hash = _world_hash(demo)
    preview = _preview(demo, tmp_path)

    status, validation = _post(
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
    status, result = _post(demo, tmp_path, "/api/v1/nl/apply", request)
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

    repeated_status, repeated = _post(demo, tmp_path, "/api/v1/nl/apply", request)
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


def test_completed_source_can_create_a_past_fork_without_changing_it(tmp_path: Path) -> None:
    demo = _demo(duration_minutes=20)
    demo.dispatch("resume", {"branch_id": "baseline"})
    demo.dispatch("advance", {"branch_id": "baseline", "steps": 20})
    source_hash = _world_hash(demo)
    preview = _preview(demo, tmp_path, selected_minute=9)

    status, result = _post(
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


def test_preview_is_stale_after_source_branch_changes_and_cannot_be_applied(tmp_path: Path) -> None:
    demo = _demo()
    preview = _preview(demo, tmp_path)
    demo.dispatch("resume", {"branch_id": "baseline"})
    demo.dispatch("advance", {"branch_id": "baseline", "steps": 1})
    demo.dispatch("pause", {"branch_id": "baseline"})
    changed_hash = _world_hash(demo)

    status, validation = _post(
        demo,
        tmp_path,
        "/api/v1/nl/validate",
        {"branch_id": "baseline", "preview_id": preview["preview_id"]},
    )
    assert status == 200
    assert validation["valid"] is False
    assert validation["stale"] is True

    status, result = _post(
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


def test_unsupported_offline_input_is_explained_and_cannot_be_approved(tmp_path: Path) -> None:
    demo = _demo()
    status, preview = _post(
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

    status, result = _post(
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
