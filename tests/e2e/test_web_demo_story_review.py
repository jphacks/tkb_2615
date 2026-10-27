"""The demo's explicit story review route works offline and does not change any Branch."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.provider import LLMRouter
from sheltersim.web.demo import DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for


@pytest.fixture
def demo(day3_demo: DemoService) -> DemoService:
    return day3_demo


def test_explicit_story_review_route_is_offline_and_does_not_mutate_branches(
    demo: DemoService, tmp_path: Path
) -> None:
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    demo.dispatch("fork", {"branch_id": "baseline", "new_branch_id": "candidate"})
    for branch_id in ("baseline", "candidate"):
        demo.dispatch("resume", {"branch_id": branch_id})
        demo.dispatch("advance", {"branch_id": branch_id, "steps": 8})
    demo._story_router = LLMRouter(LLMSettings(mode=LLMMode.OFFLINE))
    before_review = demo.state()

    handler_type = _handler_for(InspectorService(tmp_path), demo)
    handler = handler_type.__new__(handler_type)
    request = json.dumps(
        {"baseline_branch_id": "baseline", "candidate_branch_id": "candidate"}
    ).encode()
    responses: list[tuple[int, dict]] = []
    handler.path = "/api/v1/demo/story-review"
    handler.headers = {"Content-Length": str(len(request)), "Content-Type": "application/json"}
    handler.rfile = BytesIO(request)
    handler._json_response = lambda status, document: responses.append((status, document))
    handler.do_POST()

    assert responses[0][0] == 200
    assert responses[0][1]["card"]["mode"] == "offline"
    assert responses[0][1]["review_status"] == "rule_fallback"
    assert responses[0][1]["usage"]["api_request_count"] == 0
    assert demo.state() == before_review
