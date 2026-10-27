"""An agent's state holds at most one exclusive activity and a valid next decision time."""

from __future__ import annotations

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim.core.world import WorldState
from sheltersim.domain.models import Activity, AgentState
from sheltersim.experiments.scenario import ScenarioSpec


def test_agent_rejects_multiple_exclusive_activities() -> None:
    agent = next(
        iter(WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS)).agents.values())
    )

    with pytest.raises(ValueError, match="multiple exclusive activities"):
        AgentState(
            profile=agent.profile,
            zone_id=agent.zone_id,
            activity=[Activity.MOVING, Activity.QUEUED],
        )


def test_agent_rejects_invalid_next_decision_time() -> None:
    agent = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS)).agents["A001"]

    with pytest.raises(ValueError, match="next_decision_at"):
        AgentState(
            profile=agent.profile,
            zone_id=agent.zone_id,
            next_decision_at=-1,
        )
