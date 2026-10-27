"""Paths of the fixture Scenarios that the shared test setup reads."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parents[2] / "fixtures"
SMALL_REPLAY = FIXTURES / "scenarios" / "h00_small_replay.json"
SCENARIO_100_AGENTS = FIXTURES / "scenario_100_agents.json"
BASELINE_100_7D = FIXTURES / "scenarios" / "baseline_100_7d.json"
FAILURE_DAY3 = FIXTURES / "scenarios" / "failure_day3.json"
