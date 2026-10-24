"""Offline smoke checks for the fixed judge replay and loopback routes."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any

from sheltersim.experiments.comparison import compare_run_artifacts
from sheltersim.llm.provider import LLMMode, LLMRouter, LLMSettings
from sheltersim.web.demo import DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for

ROOT = Path(__file__).parents[1]
REPLAY_ROOT = ROOT / "fixtures" / "demo_replay"
RUNS = REPLAY_ROOT / "runs"
BASELINE = RUNS / "baseline"
CANDIDATE = RUNS / "day4-toilet-outage"


def test_packaged_day4_replay_is_a_paired_seven_day_branch() -> None:
    comparison = json.loads((REPLAY_ROOT / "comparison.json").read_text(encoding="utf-8"))
    baseline_manifest = json.loads((BASELINE / "manifest.json").read_text(encoding="utf-8"))
    candidate_manifest = json.loads((CANDIDATE / "manifest.json").read_text(encoding="utf-8"))
    lineage = json.loads((CANDIDATE / "branch_lineage.json").read_text(encoding="utf-8"))
    report = compare_run_artifacts(BASELINE, CANDIDATE)
    outage_events = [
        json.loads(line)
        for line in (CANDIDATE / "events_realized.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    outage_events = [
        item
        for item in outage_events
        if item["event"].get("provenance", {}).get("effect_program_v1", {}).get("op")
        == "facility_outage"
    ]

    assert comparison["execution_mode"] == "precomputed_replay"
    assert comparison["agent_count"] == 100
    assert comparison["duration_minutes"] == 10_080
    assert comparison["fork_minute"] == 5_160
    assert comparison["api_request_count"] == 0
    assert len(json.dumps(comparison)) < 20_000
    assert baseline_manifest["final_minute"] == candidate_manifest["final_minute"] == 10_080
    assert (
        baseline_manifest["execution_mode"]
        == candidate_manifest["execution_mode"]
        == "precomputed_replay"
    )
    assert candidate_manifest["parent_branch_id"] == "baseline"
    assert lineage["parent_branch_id"] == "baseline"
    assert lineage["fork_at_minute"] == 5_160
    assert report["seed"] == comparison["seed"]
    assert report["first_divergence_minute"] == comparison["first_divergence_minute"]
    assert report["first_divergence_minute"] is not None
    assert any(item["event"]["due_minute"] == 5_160 for item in outage_events)
    assert "single seed" in report["interpretation"]


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
