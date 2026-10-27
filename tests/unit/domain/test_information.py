"""The information runtime state: its summary and the migration of saved v1 state."""

from __future__ import annotations

from sheltersim.domain.information import (
    create_information_runtime_state,
    information_spread_summary,
    upgrade_information_runtime_state,
    validate_information_runtime_state,
)
from sheltersim.domain.models import (
    AgentInformationState,
    InformationBeliefStatus,
    InformationTruthStatus,
    TypedMessage,
)


def test_information_summary_separates_current_wrong_belief_from_forgotten_history() -> None:
    state = create_information_runtime_state({"A001"})
    message = TypedMessage(
        id="false-claim",
        truth_status=InformationTruthStatus.FALSE,
        source_id="source",
        zone_id="zone",
        time=0,
        origin_event_id="rumor",
        origin_agent_id="A001",
    )
    state["messages"][message.id] = message.to_dict()
    state["beliefs"]["A001"][message.id] = AgentInformationState(
        message_id=message.id,
        belief=InformationBeliefStatus.UNKNOWN,
        source="A001",
        last_contact=0,
        last_event_id="rumor",
        belief_strength=1.0,
        share_propensity=0.0,
        ever_misbelieved=True,
        ever_forgotten=True,
    ).to_dict()
    summary = information_spread_summary(state, agent_count=1)
    assert summary["wrong_belief_count"] == 0
    assert summary["current_wrong_belief_count"] == 0
    assert summary["ever_wrong_belief_count"] == 1
    assert summary["currently_forgotten_count"] == 1
    assert summary["ever_forgotten_count"] == 1


def test_information_runtime_v1_checkpoint_migrates_history_fields_and_validates() -> None:
    state = create_information_runtime_state({"A001"})
    message = TypedMessage(
        id="false-claim",
        truth_status=InformationTruthStatus.FALSE,
        source_id="source",
        zone_id="zone",
        time=0,
        origin_event_id="rumor",
        origin_agent_id="A001",
    )
    state["messages"][message.id] = message.to_dict()
    record = AgentInformationState(
        message_id=message.id,
        belief=InformationBeliefStatus.UNKNOWN,
        source="A001",
        last_contact=0,
        last_event_id="rumor",
        belief_strength=1.0,
        share_propensity=0.0,
        ever_misbelieved=True,
        ever_forgotten=True,
    ).to_dict()
    record.pop("ever_misbelieved")
    record.pop("ever_forgotten")
    record.pop("last_correction_accepted")
    state["beliefs"]["A001"][message.id] = record
    state["schema_version"] = 1

    upgrade_information_runtime_state(state)
    validate_information_runtime_state(
        state, agent_ids={"A001"}, zone_ids={"zone"}, duration_minutes=10
    )
    migrated = AgentInformationState.from_dict(state["beliefs"]["A001"][message.id])
    assert state["schema_version"] == 2
    assert migrated.ever_misbelieved is True
    assert migrated.ever_forgotten is True
