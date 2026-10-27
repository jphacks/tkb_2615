"""The saved-run inspector over HTTP: the API dispatch is allow-listed, story cards link
only recorded states, decisions and events, and mutating requests are refused.

The inspector queries themselves are checked in tests/integration/test_web_inspector.py.
"""

from __future__ import annotations

import json
from io import BytesIO

import pytest

from sheltersim.web.inspector import InspectorService, WebQueryError
from sheltersim.web.server import _dispatch_api, _handler_for


def test_agent_story_cards_link_only_recorded_states_decisions_and_events(
    full_trace_inspector: InspectorService,
) -> None:
    run_id = full_trace_inspector.list_runs()["runs"][0]["id"]

    stories = full_trace_inspector.agent_stories(run_id, at_minute=2)
    api_stories = _dispatch_api(
        full_trace_inspector,
        f"/api/v1/runs/{run_id}/agent-stories",
        {"at_minute": ["2"]},
    )

    assert stories == api_stories
    assert stories["decision_trace_available"] is True
    assert [item["criterion_id"] for item in stories["stories"]] == [
        "support_need",
        "mobility_support",
        "family_context",
    ]
    assert all(item["selection_reason"] for item in stories["stories"])
    assert all(len(item["history"]) <= 8 for item in stories["stories"])
    assert all(item["dialogue"] is None for item in stories["stories"])
    assert all(
        full_trace_inspector.event(run_id, event["event_id"])["records"]
        for story in stories["stories"]
        for event in story["event_references"]
    )
    with pytest.raises(WebQueryError, match="duration"):
        full_trace_inspector.agent_stories(run_id, at_minute=3)


def test_api_dispatch_is_allowlisted_and_reports_unknown_routes(
    full_trace_inspector: InspectorService,
) -> None:
    run_id = full_trace_inspector.list_runs()["runs"][0]["id"]
    result = _dispatch_api(
        full_trace_inspector,
        f"/api/v1/runs/{run_id}/profiles",
        {"limit": ["100"]},
    )
    encoded = json.dumps(result)
    assert "must-not-be-returned" not in encoded
    assert len(result["items"]) == 100
    with pytest.raises(WebQueryError, match="unknown API"):
        _dispatch_api(full_trace_inspector, "/api/v1/runs/nope/mutate", {})


def test_http_handler_serves_ui_and_rejects_mutating_requests(
    full_trace_inspector: InspectorService,
) -> None:
    handler_type = _handler_for(full_trace_inspector)
    handler = handler_type.__new__(handler_type)
    handler.path = "/api/v1/runs/example?mutation=1"
    handler.headers = {
        "Origin": "",
        "Host": "localhost",
        "Sec-Fetch-Site": "same-origin",
        "Content-Type": "application/json",
    }
    response = []
    handler._json_response = lambda status, document: response.append((status, document))
    handler.do_POST()
    assert response == [(405, {"error": "inspector API is read-only"})]

    static_response = handler_type.__new__(handler_type)
    static_response.send_response = lambda status: response.append((status, "static"))
    static_response.send_header = lambda *_args: None
    static_response.end_headers = lambda: None
    static_response.wfile = BytesIO()
    static_response._static_response("/admin")
    static_html = static_response.wfile.getvalue()
    assert b'data-mode="admin"' in static_html
    assert b'id="run-select"' in static_html
