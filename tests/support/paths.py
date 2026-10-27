"""Paths of the files under fixtures/ that tests read.

Test files may import these constants directly (docs/testing.md); everything else
in tests/support/ reaches them as fixtures.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parents[2] / "fixtures"
SCENARIOS = FIXTURES / "scenarios"
SCENARIO_100_AGENTS = FIXTURES / "scenario_100_agents.json"
SMALL_REPLAY = SCENARIOS / "h00_small_replay.json"
BASELINE_100_7D = SCENARIOS / "baseline_100_7d.json"
FAILURE_DAY3 = SCENARIOS / "failure_day3.json"
RESEARCH_OPERATIONAL_100_7D = SCENARIOS / "research_operational_100_7d.json"
REGRESSION_CONTRACT = FIXTURES / "regression-contract.json"
SOURCE_MANIFEST = FIXTURES / "source-manifest.json"
AGENTS_INITIAL = FIXTURES / "agents_initial.json"
PROFILES_SMALL = FIXTURES / "profiles_small.json"
GYM_LAYOUT = FIXTURES / "layouts" / "gym_synthetic.json"
DEMO_REPLAY = FIXTURES / "demo_replay"
