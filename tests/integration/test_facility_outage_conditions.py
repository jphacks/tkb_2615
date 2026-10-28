"""A facility outage the operator enters on the Project form takes the facility out of
service for its window, even when someone is using it at the start (#188).

How conditions expand into scheduled events is checked in
tests/unit/region/test_conditions.py.
"""

from __future__ import annotations

import pytest

END = 120
START, STOP = 10, 40


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#188: an outage of a facility in use fails and never takes effect",
)
def test_an_outage_of_a_toilet_in_use_still_takes_it_out_of_service(
    tmp_path, monkeypatch, generated_scenario_with, saved_timeline
):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    outage = {
        "kind": "facility_outage",
        "facility_id": "toilet_01",
        "start_minute": START,
        "end_minute": STOP,
        "units_out": 1,
    }
    scenario = generated_scenario_with(
        duration_days=END / 1440, operational_conditions={"scheduled": [outage]}
    )
    _, timeline = saved_timeline(tmp_path / "projects", scenario)
    timeline.advance("main", END, checkpoint_interval=30)
    if not timeline.seek("main", START).world.facilities["toilet_01"].active_agent_ids:
        pytest.fail("toilet_01 must be in use when the outage starts")

    units = {
        world.minute_index: world.facilities["toilet_01"].working_units
        for world in timeline.iter_worlds("main", 0, END - 1)
    }

    assert any(units[minute] == 0 for minute in range(START, STOP)), "never out of service"
    assert units[END - 1] == 1, "back in service after the window"
