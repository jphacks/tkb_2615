"""Deterministic synthetic gym-map projections for the read-only Inspector."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from sheltersim.web.inspector import InspectorService

LAYOUT = Path(__file__).parents[1] / "fixtures" / "layouts" / "gym_synthetic.json"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_run(root: Path) -> Path:
    run = root / "run-map"
    run.mkdir()
    _write_json(
        run / "manifest.json",
        {
            "scenario_id": "synthetic-map-test",
            "duration_minutes": 60,
            "final_minute": 60,
            "facilities": [
                {
                    "id": f"toilet_{index}",
                    "kind": "toilet",
                    "zone_id": "toilet",
                    "capacity": 1,
                    "working_units": 1,
                }
                for index in range(1, 5)
            ],
        },
    )
    _write_json(
        run / "agents_initial.json",
        {
            "profiles": [
                {
                    "id": f"A{index:03}",
                    "group_id": f"G{index // 5:03}",
                    "initial_zone_id": "sleeping",
                    "mobility": "independent",
                    "support_need": 0.2,
                    "stress_sensitivity": 0.5,
                    "initial_needs": {},
                }
                for index in range(1, 101)
            ]
        },
    )
    _write_json(
        run / "zones.json",
        {
            "zones": [
                {"id": zone_id, "name": zone_id}
                for zone_id in ("sleeping", "corridor", "information_desk", "toilet")
            ],
            "edges": [],
        },
    )
    _write_json(run / "events_planned.json", {"events": []})
    (run / "decisions.jsonl").write_text(
        json.dumps(
            {
                "minute_index": 10,
                "agent_id": "A001",
                "observed_state": {
                    "zone_id": "toilet",
                    "activity": "queued",
                    "stress": 0.82,
                    "queue": {"kind": "toilet", "wait_minutes": 3},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run / "events_realized.jsonl").write_text(
        json.dumps(
            {
                "realized_minute": 10,
                "status": "applied",
                "event": {
                    "event_id": "toilet-4-outage",
                    "event_type": "facility_outage",
                    "payload": {"facility_id": "toilet_4", "units_out": 1},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with (run / "metrics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "minute_index",
                "toilet_queue_length",
                "toilet_active_users",
                "toilet_working_units",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerow(
            {
                "minute_index": 0,
                "toilet_queue_length": 0,
                "toilet_active_users": 1,
                "toilet_working_units": 4,
            }
        )
        writer.writerow(
            {
                "minute_index": 10,
                "toilet_queue_length": 2,
                "toilet_active_users": 1,
                "toilet_working_units": 3,
            }
        )
    return run


def _inside(point: dict[str, Any], polygon: list[list[float]]) -> bool:
    x, y = point["x"], point["y"]
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = current
        x2, y2 = previous
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def test_map_shows_100_agents_at_their_recorded_zone_deterministically(
    tmp_path: Path,
) -> None:
    run = _write_run(tmp_path)
    service = InspectorService(tmp_path)
    before = {path.name: path.read_bytes() for path in run.iterdir()}

    first = service.zone_map("run-map", at_minute=10, selected_agent_id="A001")
    second = service.zone_map("run-map", at_minute=10, selected_agent_id="A001")
    layout = json.loads(LAYOUT.read_text(encoding="utf-8"))
    polygon_by_zone = {item["zone_id"]: item["polygon"] for item in layout["zones"]}

    assert first == second
    assert first["synthetic_layout"] is True
    assert first["layout"]["id"] == "gym-synthetic-v1"
    assert len(first["agents"]) == 100
    assert first["selected_agent"]["zone_id"] == "toilet"
    assert first["selected_agent"]["stress"] == 0.82
    assert all(_inside(agent, polygon_by_zone[agent["zone_id"]]) for agent in first["agents"])
    assert sum(zone["occupancy"] for zone in first["zones"] if zone.get("modelled", True)) == 100
    assert before == {path.name: path.read_bytes() for path in run.iterdir()}


def test_facility_outage_and_queue_kpis_follow_the_selected_minute(tmp_path: Path) -> None:
    _write_run(tmp_path)
    service = InspectorService(tmp_path)

    before_outage = service.zone_map("run-map", at_minute=9)
    during_outage = service.zone_map("run-map", at_minute=10)
    facilities_before = {item["id"]: item for item in before_outage["facilities"]}
    facilities_during = {item["id"]: item for item in during_outage["facilities"]}

    assert facilities_before["toilet_4"]["status"] == "active"
    assert facilities_during["toilet_4"]["status"] == "stopped"
    assert facilities_during["toilet_4"]["working_units"] == 0
    assert during_outage["queue_summary"] == {
        "toilet_queue_length": 2,
        "toilet_active_users": 1,
        "toilet_working_units": 3,
    }
