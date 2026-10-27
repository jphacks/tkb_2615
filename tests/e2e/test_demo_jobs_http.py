"""An advance job submitted over the demo HTTP routes runs, and its status and the job
list are served.

The jobs themselves are checked in tests/integration/test_demo_jobs.py.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

from sheltersim.web.inspector import InspectorService
from sheltersim.web.jobs import DemoJobManager
from sheltersim.web.server import _handler_for


def test_http_job_routes_return_job_contract_and_status(
    tmp_path: Path, failure_day3_demo, wait_for_job
) -> None:
    demo = failure_day3_demo()
    manager = DemoJobManager(demo, tmp_path / "jobs", chunk_minutes=2)
    handler_type = _handler_for(InspectorService(tmp_path / "runs"), demo, manager)
    handler = handler_type.__new__(handler_type)
    responses: list[tuple[int, dict]] = []
    handler._json_response = lambda status, document: responses.append((status, document))
    handler.path = "/api/v1/demo/advance-job"
    body = json.dumps({"branch_id": "baseline", "target_minute": 4}).encode()
    handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
    handler.rfile = BytesIO(body)
    try:
        handler.do_POST()
        assert responses[0][0] == 202
        job_id = responses[0][1]["job_id"]
        completed = wait_for_job(manager, job_id, {"completed"})

        responses.clear()
        handler.path = f"/api/v1/jobs/{job_id}"
        handler.do_GET()
        assert responses[0][0] == 200
        assert responses[0][1]["status"] == completed["status"]
        assert {"percent", "minute", "error"} <= responses[0][1].keys()

        responses.clear()
        handler.path = "/api/v1/jobs"
        handler.do_GET()
        assert responses[0][0] == 200
        assert responses[0][1]["jobs"][0]["job_id"] == job_id
    finally:
        manager.close()
