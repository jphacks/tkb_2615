"""An agent's state holds at most one exclusive activity and a valid next decision time."""

from __future__ import annotations

from pathlib import Path

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.models import Activity, AgentState
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[3] / "fixtures" / "scenario_100_agents.json"


def test_agent_rejects_multiple_exclusive_activities() -> None:
    agent = next(iter(WorldState.from_scenario(ScenarioSpec.load(FIXTURE)).agents.values()))

    with pytest.raises(ValueError, match="multiple exclusive activities"):
        AgentState(
            profile=agent.profile,
            zone_id=agent.zone_id,
            activity=[Activity.MOVING, Activity.QUEUED],
        )


def test_agent_rejects_invalid_next_decision_time() -> None:
    agent = WorldState.from_scenario(ScenarioSpec.load(FIXTURE)).agents["A001"]

    with pytest.raises(ValueError, match="next_decision_at"):
        AgentState(
            profile=agent.profile,
            zone_id=agent.zone_id,
            next_decision_at=-1,
        )
