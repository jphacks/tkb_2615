"""The synchronized demo timeline over HTTP picks the latest recorded minute and merges
allow-listed KPIs from saved artifacts.

Its event times, relations and agent support projections are checked in
tests/unit/web/test_inspector.py.
"""

from __future__ import annotations

import json

import pytest

from sheltersim.web.inspector import InspectorService, WebQueryError
from sheltersim.web.server import _dispatch_api


def test_timeline_selects_latest_recorded_minute_and_merges_allowlisted_kpis(
    demo_timeline_inspector: InspectorService,
) -> None:
    timeline = demo_timeline_inspector.timeline("demo-run", at_minute=3, limit=1)

    assert timeline["selected"] == {
        "minute_index": 2,
        "toilet_queue_length": 3.0,
        "stress_mean": 0.75,
        "stress_high_count": 2.0,
        "staff_busy_count": 2.0,
        "staff_load_ratio": 1.0,
        "staff_assigned_task_count": 4.0,
        "water_queue_length": 4.0,
        "meal_queue_length": 3.0,
        "water_ml_remaining": 7000.0,
        "food_meal_units_remaining": 9.0,
    }
    assert "private_column" not in json.dumps(timeline)

    routed = _dispatch_api(
        demo_timeline_inspector,
        "/api/v1/runs/demo-run/timeline",
        {"at_minute": ["3"], "limit": ["1"]},
    )
    assert routed["selected"]["minute_index"] == 2
    with pytest.raises(WebQueryError, match="at_minute"):
        demo_timeline_inspector.timeline("demo-run", at_minute=-1)
