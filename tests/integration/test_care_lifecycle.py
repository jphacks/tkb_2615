"""A generated Scenario keeps every care transition observation over a whole day.

The care receipts themselves are checked in tests/unit/domain/test_care_receipts.py.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

from sheltersim.core.engine import TraceLevel
from sheltersim.domain.care_receipts import care_history_by_agent
from sheltersim.events.queue import EventLog
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec


def test_full_day_shared_care_receipts_keep_each_transition_observation(
    generated_previews, tmp_path: Path
) -> None:
    preview = generated_previews(
        evacuees=8,
        duration_days=1,
        gross_area_m2=1200,
        accessible_toilet_count=2,
        staff_count=2,
        evacuation_selection={"age_band_multipliers": {"child": 0, "adult": 1, "older_adult": 0}},
    )

    assert preview["can_start"]
    scenario = ScenarioSpec.model_validate(copy.deepcopy(preview["scenario"]))
    world = run_scenario(scenario, tmp_path / "one-day-care", trace_level=TraceLevel.OFF)

    assert world.minute_index == 1440
    world.validate()

    # The rows a frame reuses as the EventLog grows are the rows of a full pass.
    records = world.event_log.records_from(0)
    grown = SimpleNamespace(event_log=EventLog())
    for end in range(0, len(records) + 500, 500):
        for record in records[len(grown.event_log) : end]:
            grown.event_log.append(record)
        full_pass = SimpleNamespace(event_log=copy.deepcopy(grown.event_log))
        assert care_history_by_agent(grown) == care_history_by_agent(full_pass)
    assert care_history_by_agent(grown)
