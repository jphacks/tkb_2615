"""Agents oriented to shared facilities use them while the simulation runs.

The orientation they start with is checked in tests/unit/domain/test_agent_dynamics.py.
"""

from __future__ import annotations

import csv

from sheltersim.experiments.runner import run_scenario


def test_generated_agents_use_toilets_on_the_first_day(generated_scenario, tmp_path) -> None:
    # Before orientation, every request_toilet choice was dropped because the
    # separate toilet zone was never known, so nobody was ever served.
    run_scenario(generated_scenario, tmp_path)
    with (tmp_path / "metrics.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert float(rows[-1]["toilet_wait_count"]) > 0
