"""A frozen Scenario keeps its Run ID when a saved job resumes it, and the preset catalog
and preview work through the local HTTP API without changing the live Scenario.

The editor itself is checked in tests/unit/experiments/test_scenario_editor.py.
"""

from __future__ import annotations

import json
import time
from io import BytesIO

from sheltersim.web.demo import DemoService
from sheltersim.web.inspector import InspectorService
from sheltersim.web.jobs import DemoJobManager
from sheltersim.web.server import _handler_for


def _request(**overrides: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "preset_id": "water_outage",
        "overrides": overrides,
    }


def test_frozen_scenario_restores_its_run_id_with_a_saved_job_checkpoint(tmp_path) -> None:
    scenario_path = tmp_path / ".demo-scenarios" / "frozen.json"
    job_path = tmp_path / ".demo-jobs"
    demo = DemoService(scenario_storage_path=scenario_path)
    frozen = demo.freeze_scenario(_request(staff_count=6))
    demo.dispatch("start")
    manager = DemoJobManager(demo, job_path, chunk_minutes=1)
    try:
        job = manager.submit("baseline", 1)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and manager.get(job["job_id"])["status"] not in {
            "completed",
            "failed",
        }:
            time.sleep(0.01)
        assert manager.get(job["job_id"])["status"] == "completed"
    finally:
        manager.close()

    restarted = DemoService(scenario_storage_path=scenario_path)
    recovered_manager = DemoJobManager(restarted, job_path, chunk_minutes=1)
    try:
        recovered = recovered_manager.get(job["job_id"])
        assert recovered["manifest"]["scenario_hash"] == frozen["scenario"]["scenario_hash"]
        assert recovered["manifest"]["run_id"] == frozen["scenario"]["run_id"]
        assert restarted.state()["started"] is True
        assert restarted.state()["run_id"] == frozen["scenario"]["run_id"]
    finally:
        recovered_manager.close()


def test_scenario_catalog_and_preview_are_available_through_local_api(tmp_path) -> None:
    demo = DemoService()
    original_hash = demo.state()["scenario_hash"]
    handler_type = _handler_for(InspectorService(tmp_path), demo)
    handler = handler_type.__new__(handler_type)
    responses: list[tuple[int, dict]] = []
    handler._json_response = lambda status, document: responses.append((status, document))
    handler.path = "/api/v1/scenario-presets"
    handler.do_GET()
    assert responses[0][0] == 200
    assert len(responses[0][1]["presets"]) >= 6

    responses.clear()
    body = json.dumps(_request(staff_count=6)).encode()
    handler.path = "/api/v1/demo/scenario-preview"
    handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
    handler.rfile = BytesIO(body)
    handler.do_POST()
    assert responses[0][0] == 200
    assert responses[0][1]["preview"]["agent_count"] == 100
    assert demo.state()["started"] is False
    assert demo.state()["scenario_hash"] == original_hash
