"""Social interactions are kept as structured history without utterances, and an
interaction whose state context comes from the future is refused."""

from __future__ import annotations

import json
from pathlib import Path

from sheltersim.contracts.workspace import HistoryQuery
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.domain.population_lifecycle import (
    PopulationPresenceCommand,
    apply_population_presence_command,
)
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.domain.social_dialogue import capture_social_state_context
from sheltersim.events.handlers import EventDispatcher, SocialInteractionHandler
from sheltersim.events.models import EventKind, EventStatus, ScheduledEvent
from sheltersim.experiments.branch import AddToiletCommand, BranchRuntime
from sheltersim.experiments.comparison import compare_branches
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.frames import social_event_records
from sheltersim.storage.history_index import HistoryIndex
from sheltersim.storage.projects import ProjectStore

FIXTURE = Path(__file__).parents[3] / "fixtures" / "scenario_100_agents.json"


def make_world() -> WorldState:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["duration_minutes"] = 60
    document["scheduled_events"] = []
    document["profiles"] = document["profiles"][:2]
    document["model_configs"]["policy_registry"] = {
        "schema_version": 2,
        "psychology": "life-state-feedback-v1",
    }
    document["model_configs"]["simulator_extensions"] = {"enabled": True}
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    initialize_simulator_extensions(world)
    return world


def test_structured_interaction_is_indexed_without_utterance_and_restores_social_ties(
    tmp_path: Path,
) -> None:
    world = make_world()
    actor, participant = sorted(world.agents)
    assert world.agents[actor].zone_id == world.agents[participant].zone_id
    world.minute_index = 1
    world.agents[participant].stress = 0.9
    world.model_state["body_state_v1"]["agents"][participant]["uncertainty"] = 0.8
    proposal_state = capture_social_state_context(world, (actor, participant))
    sequence = world.allocate_event_sequence()
    interaction = ScheduledEvent(
        event_id=f"social-interaction-{sequence}",
        kind=EventKind.ENDOGENOUS,
        due_minute=1,
        priority=35,
        sequence=sequence,
        event_type="social_interaction",
        payload={
            "interaction_type": "reassurance_exchange",
            "agent_id": actor,
            "participant_agent_id": participant,
            "conversation_state_context": proposal_state,
            "zone_id": world.agents[actor].zone_id,
            "topic_id": "emotional_check_in",
            "action": "offer_reassurance",
            "reason_short": "autonomous_need_choice",
            "interaction_duration_minutes": 5,
        },
        source_id="event-triggered-social-v2",
        provenance={
            "source_type": "assumption",
            "source_id": "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1",
            "policy_version": "event-triggered-social-v2",
        },
    )
    world.schedule_event(interaction)
    outcome = EventDispatcher({"social_interaction": SocialInteractionHandler()}).dispatch_due(
        world, 1
    )[0]
    assert outcome.status.value == "applied"

    relationship_key = json.dumps([actor, participant], separators=(",", ":"))
    body_relationship_key = json.dumps([participant, actor], separators=(",", ":"))
    social_before_exit = json.loads(
        json.dumps(world.model_state["social_network_v1"]["relationships"][relationship_key])
    )
    body_before_exit = json.loads(
        json.dumps(world.model_state["body_state_v1"]["relationships"][body_relationship_key])
    )
    social_events, truncated = social_event_records(world)
    assert not truncated
    assert social_events[-1]["interaction_type"] == "reassurance_exchange"
    assert social_events[-1]["topic_id"] == "emotional_check_in"
    assert social_events[-1]["actors"] == [actor, participant]

    world.minute_index = 2
    sequence = world.allocate_event_sequence()
    apply_population_presence_command(
        world,
        ScheduledEvent(
            event_id=f"temporary-exit-{sequence}",
            kind=EventKind.INTERVENTION,
            due_minute=2,
            priority=0,
            sequence=sequence,
            event_type="population_presence",
            payload={},
        ),
        PopulationPresenceCommand(
            action="temporary_exit", agent_id=actor, expected_return_minute=5
        ),
    )
    assert actor not in world.agents
    assert any(
        row["namespace"] == "social_network_v1" and row["pair_key"] == relationship_key
        for row in world.model_state["population_lifecycle_v1"]["relationship_archive"]
    )

    world.minute_index = 3
    sequence = world.allocate_event_sequence()
    apply_population_presence_command(
        world,
        ScheduledEvent(
            event_id=f"return-{sequence}",
            kind=EventKind.INTERVENTION,
            due_minute=3,
            priority=0,
            sequence=sequence,
            event_type="population_presence",
            payload={},
        ),
        PopulationPresenceCommand(action="return", agent_id=actor, zone_id="sleeping"),
    )
    assert (
        world.model_state["social_network_v1"]["relationships"][relationship_key]
        == social_before_exit
    )
    assert (
        world.model_state["body_state_v1"]["relationships"][body_relationship_key]
        == body_before_exit
    )
    world.validate()

    checkpoint = Checkpoint.from_json(Checkpoint.capture(world).to_json())
    restored = checkpoint.restore_world()
    assert restored.canonical_json() == world.canonical_json()

    store = ProjectStore(tmp_path / "social-history")
    store.create_project("project")
    store._path("project", "runs", "run").mkdir(parents=True)
    index = HistoryIndex(store, "project", "run")
    index.ingest("main", checkpoint, rebuild=True)
    index.ingest("sibling", checkpoint, rebuild=True)
    index.add_turn(
        "main",
        {
            "turn_id": "turn-1",
            "conversation_id": "conversation-1",
            "minute": 3,
            "status": "unavailable",
        },
    )
    # Rebuild may replace derived projections after a version change without
    # altering the immutable EventLog, deleting saved turns or duplicating rows.
    index.ingest("main", checkpoint, rebuild=True)
    page = index.search(
        "main",
        HistoryQuery(kind="dialogue", start=0, end=3, agent_id=actor),
        checkpoint.checkpoint_hash,
    )
    assert len(page["items"]) == 1
    row = page["items"][0]
    assert row["record_type"] == "structured_social_interaction"
    assert row["utterance_recorded"] is False
    assert row["synthetic"] is True
    assert row["interaction_type"] == "reassurance_exchange"
    assert row["virtual_utterance_policy"] == "typed-interaction-state-grounded-v3"
    assert row["virtual_utterance_example"]
    assert "少し安心できた" in row["virtual_utterance_example"]
    assert row["utterance_status"] == "not_recorded"
    assert row["virtual_utterance_label"] == "fictional_example_not_recorded_speech"
    dialogue = row["virtual_dialogue_example"]
    assert dialogue["schema_version"] == 1
    assert dialogue["label"] == "fictional_example_not_recorded_speech"
    assert [turn["speaker_id"] for turn in dialogue["turns"]] == [actor, participant]
    participant_turn = dialogue["turns"][1]
    assert participant_turn["proposal_state"]["stress"] == 0.9
    assert "stress_high_at_proposal" in participant_turn["state_cues"]
    assert "uncertainty_high_at_proposal" in participant_turn["state_cues"]
    assert "不安がありました" in participant_turn["example_text"]
    assert row["virtual_utterance_basis"] == {
        "interaction_type": "reassurance_exchange",
        "action": "offer_reassurance",
        "reason_short": "autonomous_need_choice",
        "topic_id": "emotional_check_in",
        "state_cues": ["reassurance_increased", "uncertainty_decreased"],
        "observed_model_effects": row["observed_model_effects"],
    }
    effects_by_kind = {
        effect["state_kind"]: effect["deltas"] for effect in row["observed_model_effects"]
    }
    assert effects_by_kind["relationship_update"]["relationship_trust_delta"] > 0
    assert effects_by_kind["reassurance_exchange"] == {
        "positive_affect": 0.010000000000000009,
        "reassurance": 0.01,
        "uncertainty": -0.010000000000000009,
    }
    assert "utterance" not in row
    assert "dialogue_text" not in row
    sibling = index.search(
        "sibling",
        HistoryQuery(kind="dialogue", start=0, end=3, agent_id=actor),
        checkpoint.checkpoint_hash,
    )
    assert len(sibling["items"]) == 1
    assert sibling["items"][0]["event_id"] == row["event_id"]
    conversation = index.search(
        "main",
        HistoryQuery(kind="conversations", start=0, end=3, conversation_id="conversation-1"),
        checkpoint.checkpoint_hash,
    )
    assert len(conversation["items"]) == 1
    assert conversation["items"][0]["turn_id"] == "turn-1"

    root = BranchRuntime.from_checkpoint(checkpoint, branch_id="origin")
    baseline_branch = root.fork("baseline")
    candidate_branch = root.fork("candidate")
    candidate_branch.apply(AddToiletCommand("candidate-toilet", "toilet", capacity=2))
    baseline_branch.metrics.capture(baseline_branch.world, minute=3)
    candidate_branch.metrics.capture(candidate_branch.world, minute=3)
    comparison = compare_branches(baseline_branch, candidate_branch)
    assert comparison.first_divergence_minute == 3
    assert comparison.changed_facilities[0]["id"] == "candidate-toilet"
    assert comparison.added_event_ids

    baseline_checkpoint = baseline_branch.checkpoint()
    candidate_checkpoint = candidate_branch.checkpoint()
    index.ingest("branch-baseline", baseline_checkpoint, rebuild=True)
    index.ingest("branch-candidate", candidate_checkpoint, rebuild=True)
    baseline_dialogue = index.search(
        "branch-baseline",
        HistoryQuery(kind="dialogue", start=0, end=3, agent_id=actor),
        baseline_checkpoint.checkpoint_hash,
    )["items"]
    candidate_dialogue = index.search(
        "branch-candidate",
        HistoryQuery(kind="dialogue", start=0, end=3, agent_id=actor),
        candidate_checkpoint.checkpoint_hash,
    )["items"]
    assert len(baseline_dialogue) == len(candidate_dialogue) == 1
    assert candidate_dialogue[0]["event_id"] == baseline_dialogue[0]["event_id"]
    assert candidate_dialogue[0]["virtual_dialogue_example"] == dialogue

    restored_candidate = BranchRuntime.from_checkpoint(
        candidate_checkpoint, branch_id="candidate-restored"
    )
    restored_checkpoint = restored_candidate.checkpoint()
    index.ingest("branch-restored", restored_checkpoint, rebuild=True)
    restored_dialogue = index.search(
        "branch-restored",
        HistoryQuery(kind="dialogue", start=0, end=3, agent_id=actor),
        restored_checkpoint.checkpoint_hash,
    )["items"]
    assert len(restored_dialogue) == 1
    assert restored_dialogue[0]["virtual_dialogue_example"] == dialogue


def test_social_dialogue_rejects_future_proposal_state_without_applying_effects() -> None:
    world = make_world()
    actor, participant = sorted(world.agents)
    world.minute_index = 1
    context = capture_social_state_context(world, (actor, participant))
    context["captured_minute"] = 2
    before = (world.agents[participant].stress, world.agents[participant].trust)
    sequence = world.allocate_event_sequence()
    world.schedule_event(
        ScheduledEvent(
            event_id=f"future-social-context-{sequence}",
            kind=EventKind.ENDOGENOUS,
            due_minute=1,
            priority=35,
            sequence=sequence,
            event_type="social_interaction",
            payload={
                "interaction_type": "reassurance_exchange",
                "agent_id": actor,
                "participant_agent_id": participant,
                "conversation_state_context": context,
                "zone_id": world.agents[actor].zone_id,
                "topic_id": "emotional_check_in",
                "action": "offer_reassurance",
                "reason_short": "autonomous_need_choice",
                "interaction_duration_minutes": 5,
            },
            source_id="event-triggered-social-v2",
            provenance={
                "source_type": "assumption",
                "source_id": "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1",
                "policy_version": "event-triggered-social-v2",
            },
        )
    )

    outcome = EventDispatcher({"social_interaction": SocialInteractionHandler()}).dispatch_due(
        world, 1
    )[0]

    assert outcome.status is EventStatus.FAILED
    assert "state context is invalid" in (outcome.detail or "")
    assert (world.agents[participant].stress, world.agents[participant].trust) == before
