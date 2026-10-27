"""The judge demo over loopback HTTP: the replay and admin pages are served, and a Day 4
preview can be approved, retried and reloaded offline.

That the packaged replay is a paired seven-day Branch is checked in
tests/unit/repo/test_demo_replay_fixture.py.
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any

from support.paths import DEMO_REPLAY

from sheltersim.llm.provider import LLMMode, LLMRouter, LLMSettings
from sheltersim.web.demo import DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for

RUNS = DEMO_REPLAY / "runs"


def _handler_response(handler: Any, method: str) -> tuple[int, bytes]:
    response_status: list[int] = []
    handler.send_response = response_status.append
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()
    getattr(handler, method)()
    return response_status[0], handler.wfile.getvalue()


def _get(path: str, demo: DemoService | None = None) -> tuple[int, bytes]:
    handler_type = _handler_for(InspectorService(RUNS), demo)
    handler = handler_type.__new__(handler_type)
    handler.path = path
    return _handler_response(handler, "do_GET")


def _post(path: str, body: dict[str, Any], demo: DemoService) -> tuple[int, dict[str, Any]]:
    handler_type = _handler_for(InspectorService(RUNS), demo)
    handler = handler_type.__new__(handler_type)
    raw_body = json.dumps(body).encode("utf-8")
    handler.path = path
    handler.headers = {"Content-Length": str(len(raw_body)), "Content-Type": "application/json"}
    handler.rfile = BytesIO(raw_body)
    status, raw_response = _handler_response(handler, "do_POST")
    return status, json.loads(raw_response)


def test_demo_http_routes_serve_the_replay_and_admin_page() -> None:
    status, raw_report = _get("/api/v1/demo/precomputed-comparison")
    report = json.loads(raw_report)
    assert status == 200
    assert report["candidate_run_id"] == "day4-toilet-outage"

    status, raw_page = _get(
        f"/admin?run_id=day4-toilet-outage&branch_id={report['candidate_branch_id']}&minute=5160"
    )
    page = raw_page.decode("utf-8")
    assert status == 200
    assert 'data-mode="admin"' in page
    assert 'id="run-select"' in page
    assert 'id="admin-panel-events"' in page

    status, raw_error = _get("/api/v1/demo/precomputed-comparison?path=../")
    assert status == 400
    assert (
        json.loads(raw_error)["error"] == "precomputed comparison does not accept query parameters"
    )


def test_offline_day4_preview_approval_retry_and_page_reload() -> None:
    demo = DemoService()
    demo.compiler_router = LLMRouter(LLMSettings(mode=LLMMode.OFFLINE))
    status, _ = _post("/api/v1/demo/start", {}, demo)
    assert status == 200
    status, _ = _post("/api/v1/demo/pause", {"branch_id": "baseline"}, demo)
    assert status == 200

    status, preview = _post(
        "/api/v1/nl/interpret",
        {
            "text": "Day 4 14:00 toilet_1 outage",
            "branch_id": "baseline",
            "selected_minute": 0,
        },
        demo,
    )
    assert status == 200
    assert preview["result"]["status"] == "compiled"
    assert preview["result"]["usage"]["api_request_count"] == 0
    assert preview["validation"]["valid"] is True
    status, unsupported = _post(
        "/api/v1/nl/interpret",
        {
            "text": "子どもが泣き続け避難者が眠れない",
            "branch_id": "baseline",
            "selected_minute": 0,
        },
        demo,
    )
    assert status == 200
    assert unsupported["result"]["status"] == "not_simulatable"
    assert set(demo.branches) == {"baseline"}

    approval_body = {
        "source_branch_id": "baseline",
        "preview_id": preview["preview_id"],
        "idempotency_key": "judge-day4-approval-v1",
    }
    status, first = _post("/api/v1/nl/apply", approval_body, demo)
    assert status == 200
    status, retry = _post("/api/v1/nl/apply", approval_body, demo)
    assert status == 200
    assert retry["branch_id"] == first["branch_id"]
    assert retry["idempotent_replay"] is True

    state_before_reload = json.loads(_get("/api/v1/demo", demo)[1])
    assert len(state_before_reload["branches"]) == 2
    assert _get("/demo", demo)[0] == 200
    assert _get("/admin?run_id=day4-toilet-outage&minute=5160", demo)[0] == 200
    state_after_reload = json.loads(_get("/api/v1/demo", demo)[1])
    assert state_after_reload["started"] is True
    assert [item["branch_id"] for item in state_after_reload["branches"]] == [
        "baseline",
        first["branch_id"],
    ]
