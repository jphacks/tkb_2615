"""A repeated staff absence the operator enters on the Project form takes the worker off
duty in each window and back afterwards, and nobody is assigned to them while away.

How conditions expand into scheduled events is checked in
tests/unit/region/test_conditions.py.
"""

from __future__ import annotations

from sheltersim.domain.operational import OPERATIONAL_EVENTS_KEY

END = 120


def test_a_repeated_staff_absence_takes_the_worker_off_duty_in_each_window(
    tmp_path, monkeypatch, generated_scenario_with, saved_timeline
):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    absence = {
        "kind": "staff_unavailable",
        "staff_id": "staff_001",
        "start_minute": 5,
        "end_minute": 30,
        "repeat_every_minutes": 60,
    }
    scenario = generated_scenario_with(
        duration_days=END / 1440, operational_conditions={"scheduled": [absence]}
    )
    _, timeline = saved_timeline(tmp_path / "projects", scenario)
    timeline.advance("main", END, checkpoint_interval=30)

    away = set()
    for world in timeline.iter_worlds("main", 0, END - 1):
        status = world.model_state[OPERATIONAL_EVENTS_KEY]["staff_status"]["staff_001"]
        if not status["available"]:
            away.add(world.minute_index)
            assert not [
                task for task in world.active_tasks.values() if task.staff_id == "staff_001"
            ]

    # Each event applies during its minute's tick, so the state shows from the next minute.
    assert away == set(range(6, 31)) | set(range(66, 91))
