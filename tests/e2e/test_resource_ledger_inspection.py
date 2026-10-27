"""A Run's resource ledger is saved the same at every trace level and read back
through the Run inspector and the HTTP API.

The ledger's accounting, reservations and checks are tested in
tests/unit/domain/test_resources.py.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

from sheltersim.core.engine import TraceLevel
from sheltersim.domain.resources import RESOURCE_LEDGER_KEY
from sheltersim.experiments.runner import run_scenario
from sheltersim.inspection.query import RunInspector
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _dispatch_api


def test_resource_metrics_logs_and_state_are_trace_level_independent(
    tmp_path: Path, scenario_with_stock
) -> None:
    events: list[dict[str, object]] = [
        {
            "id": "resource-reserve",
            "due_minute": 0,
            "sequence": 0,
            "event_type": "resource_reserve",
            "source_id": "ASM-H01-TEST-RESOURCE-ALLOCATION-V1",
            "payload": {
                "warehouse_id": "warehouse-main",
                "resource_id": "food_meal_units",
                "reservation_id": "meal-A001",
                "agent_id": "A001",
                "quantity": 1,
            },
        },
        {
            "id": "resource-consume",
            "due_minute": 0,
            "sequence": 1,
            "event_type": "resource_consume",
            "source_id": "ASM-H01-TEST-RESOURCE-SERVICE-V1",
            "payload": {
                "warehouse_id": "warehouse-main",
                "resource_id": "food_meal_units",
                "reservation_id": "meal-A001",
                "agent_id": "A001",
                "quantity": 1,
            },
        },
    ]
    scenario = scenario_with_stock(
        water_ml=900,
        food_meal_units=2,
        scheduled_events=events,
        duration_minutes=3,
    )
    off_directory = tmp_path / "trace-off"
    summary_directory = tmp_path / "trace-summary"
    off_world = run_scenario(scenario, off_directory, trace_level=TraceLevel.OFF)
    summary_world = run_scenario(scenario, summary_directory, trace_level=TraceLevel.SUMMARY)

    assert off_world.canonical_json() == summary_world.canonical_json()
    assert (off_directory / "events_realized.jsonl").read_bytes() == (
        summary_directory / "events_realized.jsonl"
    ).read_bytes()
    assert (off_directory / "metrics.csv").read_bytes() == (
        summary_directory / "metrics.csv"
    ).read_bytes()
    with (off_directory / "metrics.csv").open(encoding="utf-8", newline="") as stream:
        metric_rows = list(csv.DictReader(stream))
    assert metric_rows[0]["food_meal_units_initial"] == "2"
    assert metric_rows[0]["food_meal_units_served"] == "1"
    assert metric_rows[0]["food_meal_units_remaining"] == "1"
    off_manifest = json.loads((off_directory / "manifest.json").read_text(encoding="utf-8"))
    summary_manifest = json.loads((summary_directory / "manifest.json").read_text(encoding="utf-8"))
    assert off_manifest["resource_ledger_summary"] == summary_manifest["resource_ledger_summary"]
    assert off_manifest["resource_ledger_summary"]["audit_entry_count"] == 4
    assert off_manifest["initial_stock"]["source_ids"] == ["ASM-H01-TEST-INITIAL-STOCK-V1"]
    assert (off_directory / "resource_ledger.json").is_file()
    ledger = json.loads((off_directory / "resource_ledger.json").read_text(encoding="utf-8"))
    assert ledger == off_world.model_state[RESOURCE_LEDGER_KEY]
    assert ledger["warehouse_id"] == "warehouse-main"
    query = RunInspector(off_directory).resource_ledger()
    assert query == ledger
    run_id = off_manifest["run_id"]
    runs_directory = tmp_path / "runs"
    runs_directory.mkdir()
    shutil.copytree(off_directory, runs_directory / run_id)
    web_inspector = InspectorService(runs_directory)
    assert web_inspector.resource_ledger(run_id) == {"enabled": True, "ledger": ledger}
    assert _dispatch_api(web_inspector, f"/api/v1/runs/{run_id}/resource-ledger", {}) == {
        "enabled": True,
        "ledger": ledger,
    }
    assert (
        web_inspector.overview(run_id)["manifest"]["resource_ledger_summary"]["zone_id"]
        == "sleeping"
    )
