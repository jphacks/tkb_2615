"""Read-only Inspector projections: the deterministic map (the Run's floor plan, or the
synthetic gym without one), and the demo timeline's event times, relations and agent
support from saved artifacts.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from support.paths import GYM_LAYOUT

from sheltersim.web.inspector import InspectorService


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
    layout = json.loads(GYM_LAYOUT.read_text(encoding="utf-8"))
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


def _add_floor_plan(run: Path) -> dict[str, Any]:
    """Record a 20 m x 10 m floor plan with the four zones of the hand-written Run."""
    rectangles = {
        "corridor": (0, 0, 20, 2),
        "sleeping": (0, 2, 12, 10),
        "information_desk": (12, 2, 16, 10),
        "toilet": (16, 2, 20, 10),
    }
    geometry = {
        "width_m": 20,
        "height_m": 10,
        "source_kind": "synthetic",
        "zones": [
            {
                "zone_id": zone_id,
                "polygon": [
                    {"x": left, "y": top},
                    {"x": right, "y": top},
                    {"x": right, "y": bottom},
                    {"x": left, "y": bottom},
                ],
                "usable_area_m2": (right - left) * (bottom - top),
            }
            for zone_id, (left, top, right, bottom) in rectangles.items()
        ],
        "facility_points": {
            f"toilet_{index}": {"x": 16.5 + index * 0.5, "y": 9} for index in range(1, 5)
        },
    }
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model_configs"] = {"facility_geometry": geometry}
    _write_json(manifest_path, manifest)
    return geometry


def test_a_run_with_a_floor_plan_is_drawn_from_it(tmp_path: Path) -> None:
    """Not from the bundled gym, whose zones need not match the Run's (#161)."""
    run = _write_run(tmp_path)
    _add_floor_plan(run)

    map_data = InspectorService(tmp_path).zone_map("run-map", at_minute=10)

    assert map_data["layout"]["id"] == "facility-geometry"
    assert map_data["synthetic_layout"] is False
    polygons = {zone["id"]: zone["polygon"] for zone in map_data["zones"]}
    assert set(polygons) == {"corridor", "sleeping", "information_desk", "toilet"}
    assert all(zone.get("modelled", True) for zone in map_data["zones"])
    # 48 px per m fits 20 m into 1000 px with 20 px margins; 10 m is centered in 560 px.
    assert polygons["corridor"] == [[20.0, 40.0], [980.0, 40.0], [980.0, 136.0], [20.0, 136.0]]
    assert len(map_data["agents"]) == 100
    assert map_data["undrawn_agents"] == []
    assert all(_inside(agent, polygons[agent["zone_id"]]) for agent in map_data["agents"])
    toilet_1 = next(item for item in map_data["facilities"] if item["id"] == "toilet_1")
    assert (toilet_1["x"], toilet_1["y"]) == (836.0, 472.0)


def test_agents_in_a_zone_the_map_cannot_draw_are_reported(tmp_path: Path) -> None:
    """The gym has no shape for food, so A001 there is listed instead of dropped (#161)."""
    run = _write_run(tmp_path)
    decision = json.loads((run / "decisions.jsonl").read_text(encoding="utf-8"))
    decision["observed_state"]["zone_id"] = "food"
    _write_json(run / "decisions.jsonl", decision)

    map_data = InspectorService(tmp_path).zone_map("run-map", at_minute=10)

    assert map_data["layout"]["id"] == "gym-synthetic-v1"
    assert len(map_data["agents"]) == 99
    assert map_data["undrawn_agents"] == [{"id": "A001", "zone_id": "food"}]


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


def test_event_times_relations_and_agent_support_are_safe_projections(
    demo_timeline_inspector: InspectorService,
) -> None:
    events = demo_timeline_inspector.events("demo-run")["items"]
    planned = next(item for item in events if item["event_id"] == "planned-water")
    realized = next(item for item in events if item["event_id"] == "realized-water")

    assert (planned["created_minute"], planned["scheduled_minute"], planned["realized_minute"]) == (
        None,
        4,
        None,
    )
    assert (
        realized["created_minute"],
        realized["scheduled_minute"],
        realized["realized_minute"],
    ) == (
        2,
        1,
        3,
    )
    assert planned["related_agent_id"] == "A_001"
    assert planned["related_zone_id"] == "sleeping"
    assert planned["related_facility_id"] == "water_station_main"
    assert "should-not-leak" not in json.dumps(events)
    assert "payload" not in planned

    initial = demo_timeline_inspector.agent("demo-run", "A_001", at_minute=0)
    assert initial["state"]["stress"] is None

    initial_map = demo_timeline_inspector.zone_map("demo-run", at_minute=0)
    initial_point = next(item for item in initial_map["agents"] if item["id"] == "A_001")
    assert initial_point["stress"] is None
    assert initial_point["queue_wait_minutes"] is None

    agent = demo_timeline_inspector.agent("demo-run", "A_001", at_minute=3)
    assert agent["state"]["queue_kind"] == "water"
    assert agent["state"]["queue_wait_minutes"] == 12
    assert agent["state"]["support_status"] == "pending"
    assert agent["state"]["support_wait_minutes"] == 7

    map_data = demo_timeline_inspector.zone_map("demo-run", at_minute=3)
    point = next(item for item in map_data["agents"] if item["id"] == "A_001")
    assert point["support_status"] == "pending"
    assert point["support_wait_minutes"] == 7
