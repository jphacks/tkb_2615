"""The Project screen's main flow over HTTP, from the structured form to the comparison.

Each request is one the demo-v2 screen sends (demo-v2.js), to a real HTTP
server on a loopback port: preview a generated Scenario, approve and run it,
preview and approve an intervention typed as text, run both Branches, and
read the comparison. Only the regional research is replaced, as in the shared
fixtures (tests/conftest.py ui_project_api); the intervention text is read by
the offline rules.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pytest

from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _handler_for

ROOT = Path(__file__).parents[2]
REGRESSION_CONTRACT = ROOT / "fixtures" / "regression-contract.json"
FORK_MINUTE = 60
COMPARE_MINUTE = 360


class _Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def get(self, path: str) -> dict[str, Any]:
        with urlopen(self.base + path, timeout=60) as response:
            return json.loads(response.read())

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read())

    def run_until(self, run: str, branch_id: str, minute: int) -> dict[str, Any]:
        """Ask the background worker to compute a Branch, then poll as the screen does."""
        self.post(f"{run}/resume", {"branch_id": branch_id, "target_minute": minute})
        deadline = time.monotonic() + 300
        while (state := self.get(run))["status"] == "running":
            assert time.monotonic() < deadline, f"{branch_id} did not reach minute {minute}"
            time.sleep(0.1)
        assert state["status"] in {"paused", "completed"}
        return state


@pytest.fixture
def client(ui_project_api, tmp_path) -> Iterator[_Client]:
    server = HTTPServer(
        ("127.0.0.1", 0), _handler_for(InspectorService(tmp_path / "runs"), projects=ui_project_api)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Client(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _computed_until(run_state: dict[str, Any], branch_id: str) -> int:
    return next(
        branch["computed_until"]
        for branch in run_state["branches"]
        if branch["branch_id"] == branch_id
    )


def test_arrivals_typed_on_the_project_screen_show_in_the_comparison(
    client, generated_request
) -> None:
    client.post("/api/v1/projects", {"project_id": "shelter"})
    project = "/api/v1/projects/shelter"
    run = f"{project}/runs/run"

    preview = client.post(
        f"{project}/scenario-preview", {"text": "", "request": generated_request()}
    )
    assert preview["can_start"] is True
    client.post(
        f"{project}/scenario-approve",
        {
            "preview_id": preview["preview_id"],
            "approved_hash": preview["approved_hash"],
            "approved": True,
            "run_id": "run",
        },
    )
    assert _computed_until(client.run_until(run, "main", FORK_MINUTE), "main") == FORK_MINUTE

    intervention = client.post(
        f"{run}/branches/main/intervention-preview",
        {"text": "避難スペースに避難者を10人追加", "minute": FORK_MINUTE},
    )
    assert intervention["status"] == "compiled"
    assert intervention["can_apply"] is True
    [effect] = intervention["program"]["effects"]
    assert (effect["op"], effect["target_selector"]["target_id"]) == ("add_evacuees", "sleeping")
    client.post(
        f"{run}/branches/main/intervention-apply",
        {
            "preview_id": intervention["preview_id"],
            "approved_program_hash": intervention["approved_program_hash"],
            "approved": True,
            "child_id": "arrivals",
        },
    )
    client.run_until(run, "arrivals", COMPARE_MINUTE)
    state = client.run_until(run, "main", COMPARE_MINUTE)
    assert _computed_until(state, "arrivals") == COMPARE_MINUTE

    comparison = client.get(f"{run}/compare?branch_ids=main,arrivals&minute={COMPARE_MINUTE}")

    assert comparison["shared_origin"] is True
    assert comparison["common_range"] == [FORK_MINUTE, COMPARE_MINUTE]
    main, arrivals = comparison["frames"]
    # The main line run over HTTP ends where the pinned run of the same form ends.
    contract = json.loads(REGRESSION_CONTRACT.read_text(encoding="utf-8"))["generated_small"]
    assert main["state_hash"] == contract["expected_final_state_sha256"]
    assert arrivals["kpis"]["population"] == main["kpis"]["population"] + 10
    [difference] = comparison["differences"]
    assert difference["branch_id"] == "arrivals"
    assert difference["metrics"]["population"] == 10
    assert difference["metrics"]["toilet_wait_total_minutes"] > 0
    assert difference["metrics"]["water_ml_remaining"] < 0
