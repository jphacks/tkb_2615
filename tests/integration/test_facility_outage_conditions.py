"""A facility outage or equipment failure the operator enters on the Project form takes
the toilet out of service at its start, even while someone is using it: that person
finishes, nobody starts until the unit is back, and a Checkpoint taken while they
finish resumes to the same world. Before #188 the outage failed and never took effect.

How conditions expand into scheduled events is checked in
tests/unit/region/test_conditions.py.
"""

from __future__ import annotations

import json

import pytest

from sheltersim.experiments.timeline import TimelineStore

END = 120
START, STOP = 10, 40


@pytest.mark.parametrize("kind", ["facility_outage", "equipment_failure"])
def test_an_outage_of_a_toilet_in_use_lets_its_user_finish_and_admits_nobody(
    tmp_path, monkeypatch, generated_scenario_with, saved_timeline, kind
):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    outage = {
        "kind": kind,
        "facility_id": "toilet_01",
        "start_minute": START,
        "end_minute": STOP,
        "units_out": 1,
    }
    scenario = generated_scenario_with(
        duration_days=END / 1440, operational_conditions={"scheduled": [outage]}
    )
    store, timeline = saved_timeline(tmp_path / "projects", scenario)
    world = timeline.advance("main", END, checkpoint_interval=30).world
    user = timeline.seek("main", START).world.facilities["toilet_01"].active_agent_ids
    if not user:
        pytest.fail("toilet_01 must be in use when the outage starts")

    rows = {
        item.minute_index: (
            item.facilities["toilet_01"].working_units,
            tuple(item.facilities["toilet_01"].active_agent_ids),
        )
        for item in timeline.iter_worlds("main", 0, END - 1)
    }

    # The event applies during its minute's tick, so the state shows from the next minute.
    assert all(rows[minute][0] == 0 for minute in range(START + 1, STOP + 1))
    assert rows[STOP + 1][0] == 1, "back in service after the window"
    finishing = [minute for minute in range(START + 1, STOP + 1) if rows[minute][1]]
    assert finishing, "the user at the start is still being served when the outage applies"
    assert all(rows[minute][1] == tuple(user) for minute in finishing)
    assert finishing == list(range(START + 1, finishing[-1] + 1)), "nobody starts while out"

    # A Checkpoint taken while the user finishes on a unit out of service resumes exactly.
    timeline.fork("main", finishing[0], "resumed")
    resumed = TimelineStore(store, "p", "r").advance("resumed", END, checkpoint_interval=END)
    assert json.dumps(resumed.world.to_dict(), sort_keys=True) == json.dumps(
        world.to_dict(), sort_keys=True
    )
