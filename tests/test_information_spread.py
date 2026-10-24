"""Opt-in, evidence-linked information diffusion and inquiry behavior tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.models import InformationSpreadConfig
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.report import _ASSUMPTION_LABELS, scenario_assumption_ids
from sheltersim.policies.behavior import UtilityBehaviorPolicy, WorldView
from sheltersim.policies.information import InformationSpreadPolicy

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "scenario_100_agents.json"


def _information_config(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "enabled": True,
        "max_contacts_per_agent": 16,
        "minimum_contact_minutes": 1,
        "contact_duration_scale_minutes": 1,
        "base_transmission_probability": 1.0,
        "correction_transmission_multiplier": 1.0,
        "trust_sensitivity": 0.0,
        "family_contact_multiplier": 1.0,
        "adjacent_zone_contact_multiplier": 1.0,
        "query_pressure_per_claim": 0.5,
        "query_pressure_decay_per_minute": 0.0,
        "query_pressure_relief_per_correction": 0.5,
        "query_threshold": 0.5,
        "query_utility_weight": 4.0,
    }
    values.update(overrides)
    return values


def _scenario(
    *,
    duration: int = 6,
    profile_count: int = 2,
    truth_status: str = "unknown",
    correction: bool = True,
    config: dict[str, object] | None = None,
) -> ScenarioSpec:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["id"] = "information-spread-test"
    document["duration_minutes"] = duration
    document["scheduled_events"] = [
        {
            "id": "rumor-event",
            "kind": "exogenous",
            "due_minute": 0,
            "priority": 0,
            "sequence": 10,
            "event_type": "information_rumor",
            "payload": {
                "message_id": "claim-water-1",
                "truth_status": truth_status,
                "origin_agent_id": "A001",
            },
            "source_id": "scenario-rumor-source",
        }
    ]
    if correction:
        document["scheduled_events"].append(
            {
                "id": "correction-event",
                "kind": "exogenous",
                "due_minute": 3,
                "priority": 0,
                "sequence": 11,
                "event_type": "information_correction",
                "payload": {
                    "message_id": "claim-water-1",
                    "zone_id": "sleeping",
                    "belief": "rejected" if truth_status == "false" else "verified",
                },
                "source_id": "staff-notice-source",
                "causation_id": "rumor-event",
            }
        )
    document["profiles"] = document["profiles"][:profile_count]
    for profile in document["profiles"]:
        profile["support_need"] = 0.0
        profile["initial_needs"] = {
            "toilet_urgency": 0.0,
            "hunger": 0.0,
            "fatigue": 0.0,
        }
    document["model_configs"]["information_spread"] = config or _information_config()
    document["model_configs"].setdefault("behavior", {})["temperature"] = 0.01
    return ScenarioSpec.model_validate(document)


def test_information_config_is_strict_and_off_by_default() -> None:
    config = InformationSpreadConfig.from_mapping()

    assert not config.enabled
    assert config.provenance()["source_id"] == "ASM-INFORMATION-SPREAD-V1"
    with pytest.raises(ValueError, match="unknown information_spread settings"):
        InformationSpreadConfig.from_mapping({"enabled": True, "free_text": "unsafe"})
    with pytest.raises(ValueError, match="max_contacts_per_agent"):
        InformationSpreadConfig.from_mapping({"max_contacts_per_agent": 17})

    baseline_world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    proposal = UtilityBehaviorPolicy().propose(
        WorldView.from_world(baseline_world), "A001", CounterRNG(baseline_world.seed)
    )
    help_candidate = next(
        candidate for candidate in proposal.candidates if candidate.action.value == "seek_help"
    )
    assert help_candidate.trigger == (
        "support_need>=0.25 and maximum need/stress>=0.50 "
        "and no_open_support_request and support_cooldown_elapsed"
    )


def test_transmission_probability_depends_on_trust_and_contact_duration() -> None:
    config = InformationSpreadConfig.from_mapping(
        _information_config(
            base_transmission_probability=0.5,
            trust_sensitivity=1.0,
            contact_duration_scale_minutes=5,
        )
    )
    policy = InformationSpreadPolicy(config)

    low_trust = policy._transmission_probability(0.0, "same_zone", 5, correction=False)
    short_trusted_contact = policy._transmission_probability(1.0, "same_zone", 1, correction=False)
    long_trusted_contact = policy._transmission_probability(1.0, "same_zone", 5, correction=False)

    assert low_trust == 0.0
    assert short_trusted_contact == 0.2
    assert long_trusted_contact == 1.0


def test_scenario_rejects_information_events_without_opt_in_or_causal_correction() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["scheduled_events"] = [
        {
            "id": "rumor-event",
            "due_minute": 0,
            "sequence": 1,
            "event_type": "information_rumor",
            "payload": {
                "message_id": "claim-1",
                "truth_status": "true",
                "origin_agent_id": "A001",
            },
            "source_id": "scenario-source",
        }
    ]

    with pytest.raises(ValidationError, match="requires enabled information_spread"):
        ScenarioSpec.model_validate(document)

    scenario = _scenario(truth_status="true")
    changed = scenario.model_dump(mode="json")
    changed["scheduled_events"][1]["causation_id"] = "other-event"
    with pytest.raises(ValidationError, match="causally reference an earlier rumor"):
        ScenarioSpec.model_validate(changed)


def test_rumor_spreads_only_through_local_contacts_and_correction_resolves_truth(
    tmp_path: Path,
) -> None:
    scenario = _scenario(duration=2, profile_count=4, correction=False)
    document = scenario.model_dump(mode="json")
    document["profiles"][2]["initial_zone_id"] = "corridor"
    document["profiles"][2]["group_id"] = "G003"
    document["profiles"][3]["initial_zone_id"] = "toilet"
    document["profiles"][3]["group_id"] = "G004"
    scenario = ScenarioSpec.model_validate(document)

    world = run_scenario(scenario, tmp_path / "summary", trace_level=TraceLevel.SUMMARY)
    info = world.model_state[INFORMATION_SPREAD_STATE_KEY]

    assert info["messages"]["claim-water-1"]["truth_status"] == "unknown"
    assert info["beliefs"]["A001"]["claim-water-1"]["belief"] == "heard"
    assert info["beliefs"]["A002"]["claim-water-1"]["belief"] == "heard"
    assert info["beliefs"]["A003"]["claim-water-1"]["belief"] == "heard"
    assert "claim-water-1" not in info["beliefs"]["A004"]
    assert info["counters"]["contact_deliveries"] >= 1
    local_traces = [
        json.loads(line)
        for line in (tmp_path / "summary" / "information_contacts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    adjacent_contact = next(
        contact
        for trace in local_traces
        for contact in trace["contacts"]
        if contact["recipient_agent_id"] == "A003"
        and contact["contact_basis"] == "adjacent_zone"
        and contact["transmission_selected"]
    )
    assert adjacent_contact["message_source_id"] == "scenario-rumor-source"
    assert adjacent_contact["causation_id"] == "rumor-event"

    correction_scenario = _scenario(duration=6, profile_count=2)
    corrected = run_scenario(
        correction_scenario,
        tmp_path / "correction-full",
        trace_level=TraceLevel.FULL,
    )
    corrected_info = corrected.model_state[INFORMATION_SPREAD_STATE_KEY]
    assert corrected_info["messages"]["claim-water-1"]["truth_status"] == "true"
    assert all(
        agent_beliefs["claim-water-1"]["belief"] == "verified"
        for agent_beliefs in corrected_info["beliefs"].values()
    )
    assert corrected_info["counters"]["correction_deliveries"] >= 2

    message = corrected_info["messages"]["claim-water-1"]
    assert message["source_id"] == "scenario-rumor-source"
    assert message["origin_event_id"] == "rumor-event"
    realized = {record.event.event_id: record for record in corrected.event_log.records}
    assert realized["rumor-event"].status.value == "applied"
    contact = next(
        record
        for record in realized.values()
        if record.event.event_type == "information_contact_update"
        and record.event.payload["recipient_agent_id"] == "A002"
    )
    assert contact.status.value == "applied"
    assert contact.event.source_id == "scenario-rumor-source"
    assert contact.event.causation_id == "rumor-event"
    assert realized["correction-event"].event.source_id == "staff-notice-source"
    assert realized["correction-event"].event.causation_id == "rumor-event"
    corrected_origin = corrected_info["beliefs"]["A001"]["claim-water-1"]
    assert corrected_origin["source"] == "staff-notice-source"
    assert corrected_origin["last_event_id"] == "correction-event"
    assert corrected_info["beliefs"]["A002"]["claim-water-1"]["source"] == ("staff-notice-source")

    # Inquiry pressure made a real support request and the trace links both records.
    request = next(
        record for record in corrected_info["support_requests"] if record["agent_id"] == "A001"
    )
    assert request["request_id"] in corrected.support_requests
    assert request["message_ids"] == ["claim-water-1"]
    assert (
        Checkpoint.capture(corrected).restore_world().canonical_json() == corrected.canonical_json()
    )

    artifact_dir = tmp_path / "correction-full"
    artifact = json.loads((artifact_dir / "information_spread.json").read_text())
    assert artifact["summary"]["message_count"] == 1
    assert artifact["source_id"] == "ASM-INFORMATION-SPREAD-V1"
    manifest = json.loads((artifact_dir / "manifest.json").read_text())
    assert (
        manifest["model_assumptions"]["information_spread"]["source_id"]
        == "ASM-INFORMATION-SPREAD-V1"
    )
    assert "ASM-INFORMATION-SPREAD-V1" in scenario_assumption_ids(correction_scenario)
    assert "ASM-INFORMATION-SPREAD-V1" in _ASSUMPTION_LABELS
    decisions = [
        json.loads(line)
        for line in (artifact_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    origin_decision = next(
        item for item in decisions if item["agent_id"] == "A001" and item["minute_index"] == 0
    )
    seek_help = next(
        item for item in origin_decision["candidates"] if item["action"] == "seek_help"
    )
    assert seek_help["eligible"] is True
    assert "information query pressure" in seek_help["trigger"]
    traces = [
        json.loads(line)
        for line in (artifact_dir / "information_contacts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(traces) == correction_scenario.duration_minutes


def test_trace_level_does_not_change_information_state_and_default_run_stays_off(
    tmp_path: Path,
) -> None:
    scenario = _scenario(duration=6, correction=False)
    traced = run_scenario(scenario, tmp_path / "traced", trace_level=TraceLevel.FULL)
    untraced = run_scenario(scenario, tmp_path / "untraced", trace_level=TraceLevel.OFF)
    assert traced.canonical_json() == untraced.canonical_json()

    baseline_document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    baseline_document["id"] = "information-disabled-baseline"
    baseline_document["duration_minutes"] = 2
    baseline_document["scheduled_events"] = []
    baseline_document["profiles"] = baseline_document["profiles"][:2]
    baseline = ScenarioSpec.model_validate(baseline_document)
    baseline_world = run_scenario(
        baseline,
        tmp_path / "baseline",
        trace_level=TraceLevel.OFF,
    )

    assert INFORMATION_SPREAD_STATE_KEY not in baseline_world.model_state
    assert not (tmp_path / "baseline" / "information_spread.json").exists()
    assert not (tmp_path / "baseline" / "information_contacts.jsonl").exists()
    assert (
        "information_spread"
        not in json.loads((tmp_path / "baseline" / "manifest.json").read_text())[
            "model_assumptions"
        ]
    )


def test_world_rejects_invalid_persisted_information_state() -> None:
    world = WorldState.from_scenario(_scenario())
    world.model_state[INFORMATION_SPREAD_STATE_KEY]["query_pressure"]["A001"] = 1.1

    with pytest.raises(StateInvariantError, match="query pressure"):
        world.validate()


def test_false_rumor_is_rejected_by_a_causal_correction(tmp_path: Path) -> None:
    world = run_scenario(
        _scenario(duration=4, truth_status="false"),
        tmp_path / "false-rumor",
        trace_level=TraceLevel.OFF,
    )
    info = world.model_state[INFORMATION_SPREAD_STATE_KEY]

    assert info["messages"]["claim-water-1"]["truth_status"] == "false"
    assert all(
        beliefs["claim-water-1"]["belief"] == "rejected" for beliefs in info["beliefs"].values()
    )


def test_false_rumor_flows_through_integrated_psychology_trace(tmp_path: Path) -> None:
    document = _scenario(duration=3, correction=False, truth_status="false").model_dump(mode="json")
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.01,
        "baseline_distress_by_agent": {},
        "chronic_retention": 0.9,
        "chronic_acute_weight": 0.08,
        "chronic_high_distress_weight": 0.02,
        "high_distress_threshold": 0.7,
    }
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "mean-reverting-life-state-v1",
        "information": "evidence-aware-rumor-v1",
    }
    document["model_configs"]["information_spread"].update(
        rumor_belief_probability=1.0,
        rumor_trust_sensitivity=0.0,
        rumor_source_sensitivity=0.0,
    )
    scenario = ScenarioSpec.model_validate(document)
    run_dir = tmp_path / "rumor-psychology-integration"

    run_scenario(scenario, run_dir, trace_level=TraceLevel.FULL)

    rows = [
        json.loads(line)
        for line in (run_dir / "psychology_contributions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    origin_trace = next(
        row["agents"][0]
        for row in rows
        if row["minute_index"] == 0 and row["agents"][0]["agent_id"] == "A001"
    )
    assert origin_trace["information_belief_effect"]["active_false_claim_pressure"] > 0.0
    assert origin_trace["stress"]["components"]["information_belief_via_uncertainty"] > 0.0
    assert (
        origin_trace["information_belief_effect"]["source_records"][0]["origin_event_id"]
        == "rumor-event"
    )


def test_accepted_correction_reduces_integrated_false_belief_pressure(
    tmp_path: Path,
) -> None:
    traced_scenarios: dict[str, dict[int, dict[str, object]]] = {}
    corrected_world = None
    for name, correction in (("rumor-only", False), ("rumor-corrected", True)):
        document = _scenario(
            duration=5,
            correction=correction,
            truth_status="false",
            config=_information_config(
                max_contacts_per_agent=0,
                rumor_belief_probability=1.0,
                rumor_trust_sensitivity=0.0,
                rumor_source_sensitivity=0.0,
                correction_acceptance_probability=0.95,
                correction_trust_sensitivity=0.0,
                correction_source_sensitivity=0.0,
            ),
        ).model_dump(mode="json")
        document["model_configs"]["psychology"] = {
            "natural_recovery_rate_per_minute": 0.01,
            "baseline_distress_by_agent": {},
            "chronic_retention": 0.9,
            "chronic_acute_weight": 0.08,
            "chronic_high_distress_weight": 0.02,
            "high_distress_threshold": 0.7,
        }
        document["model_configs"]["policy_registry"] = {
            "schema_version": 1,
            "psychology": "mean-reverting-life-state-v1",
            "information": "evidence-aware-rumor-v1",
        }
        scenario = ScenarioSpec.model_validate(document)
        run_dir = tmp_path / name
        world = run_scenario(scenario, run_dir, trace_level=TraceLevel.FULL)
        rows = [
            json.loads(line)
            for line in (run_dir / "psychology_contributions.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        traced_scenarios[name] = {
            row["minute_index"]: next(
                agent for agent in row["agents"] if agent["agent_id"] == "A001"
            )
            for row in rows
        }
        if correction:
            corrected_world = world

    assert corrected_world is not None
    belief = corrected_world.model_state[INFORMATION_SPREAD_STATE_KEY]["beliefs"]["A001"]
    assert belief["claim-water-1"]["last_correction_accepted"] is True
    initial = traced_scenarios["rumor-corrected"][0]
    after_correction = traced_scenarios["rumor-corrected"][3]
    control = traced_scenarios["rumor-only"][3]
    assert initial["information_belief_effect"]["active_false_claim_pressure"] > 0.0
    assert initial["stress"]["components"]["information_belief_via_uncertainty"] > 0.0
    assert "information_belief_effect" not in after_correction
    assert after_correction["stress"]["after"] < control["stress"]["after"]


def test_refused_correction_does_not_relieve_integrated_uncertainty(
    tmp_path: Path,
) -> None:
    outcomes: dict[str, dict[int, dict[str, object]]] = {}
    refused_world = None
    for name, correction in (("rumor-only", False), ("rumor-refused", True)):
        document = _scenario(
            duration=5,
            correction=correction,
            truth_status="false",
            config=_information_config(
                max_contacts_per_agent=0,
                rumor_belief_probability=1.0,
                rumor_trust_sensitivity=0.0,
                rumor_source_sensitivity=0.0,
                correction_acceptance_probability=0.0,
                correction_trust_sensitivity=0.0,
                correction_source_sensitivity=0.0,
            ),
        ).model_dump(mode="json")
        document["model_configs"]["psychology"] = {
            "natural_recovery_rate_per_minute": 0.01,
            "baseline_distress_by_agent": {},
            "chronic_retention": 0.9,
            "chronic_acute_weight": 0.08,
            "chronic_high_distress_weight": 0.02,
            "high_distress_threshold": 0.7,
        }
        document["model_configs"]["policy_registry"] = {
            "schema_version": 1,
            "psychology": "mean-reverting-life-state-v1",
            "information": "evidence-aware-rumor-v1",
        }
        world = run_scenario(
            ScenarioSpec.model_validate(document),
            tmp_path / name,
            trace_level=TraceLevel.FULL,
        )
        traces = [
            json.loads(line)
            for line in (tmp_path / name / "psychology_contributions.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        outcomes[name] = {
            row["minute_index"]: next(
                agent for agent in row["agents"] if agent["agent_id"] == "A001"
            )
            for row in traces
        }
        if correction:
            refused_world = world

    assert refused_world is not None
    belief = refused_world.model_state[INFORMATION_SPREAD_STATE_KEY]["beliefs"]["A001"][
        "claim-water-1"
    ]
    assert belief["correction_exposed"] is True
    assert belief["correction_accepted"] is False
    assert belief["last_correction_accepted"] is False
    correction_history = [
        item
        for item in refused_world.model_state["body_state_v1"]["history"]
        if item["kind"] == "information_correction"
    ]
    assert correction_history == []
    assert (
        outcomes["rumor-refused"][3]["stress"]["after"]
        == pytest.approx(outcomes["rumor-only"][3]["stress"]["after"])
    )


def test_evidence_aware_correction_tracks_exposure_and_probabilistic_acceptance(
    tmp_path: Path,
) -> None:
    scenario = _scenario(
        duration=6,
        profile_count=100,
        truth_status="false",
        config=_information_config(
            correction_acceptance_probability=0.5,
            correction_trust_sensitivity=0.0,
            correction_source_sensitivity=0.0,
            share_propensity_decay_per_minute=0.2,
            forgetting_rate_per_minute=0.3,
        ),
    )
    document = scenario.model_dump(mode="json")
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "information": "evidence-aware-rumor-v1",
    }
    scenario = ScenarioSpec.model_validate(document)

    first = run_scenario(scenario, tmp_path / "evidence-aware-1")
    second = run_scenario(scenario, tmp_path / "evidence-aware-2")
    first_state = first.model_state[INFORMATION_SPREAD_STATE_KEY]
    second_state = second.model_state[INFORMATION_SPREAD_STATE_KEY]
    records = [
        beliefs["claim-water-1"]
        for beliefs in first_state["beliefs"].values()
        if "claim-water-1" in beliefs
    ]

    assert records
    assert all(record["exposed"] for record in records)
    assert any(record["belief_strength"] == 0.0 for record in records)
    assert any(record["belief_strength"] == 1.0 for record in records)
    assert all(record["correction_exposed"] for record in records)
    assert any(record["correction_accepted"] for record in records)
    assert any(not record["correction_accepted"] for record in records)
    assert any(record["belief"] == "unknown" for record in records)
    assert any(record["share_propensity"] == 0.0 for record in records)
    assert first_state == second_state


def test_cumulative_correction_acceptance_does_not_reapply_a_later_refused_event(
    tmp_path: Path,
) -> None:
    scenario = _scenario(
        duration=7,
        profile_count=2,
        truth_status="false",
        config=_information_config(
            correction_acceptance_probability=0.5,
            correction_trust_sensitivity=0.0,
            correction_source_sensitivity=0.5,
            correction_source_reliability_by_id={
                "staff-notice-source": 1.0,
                "untrusted-staff-source": 0.0,
            },
        ),
    )
    document = scenario.model_dump(mode="json")
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "life-state-feedback-v1",
        "information": "evidence-aware-rumor-v1",
    }
    document["scheduled_events"].append(
        {
            "id": "correction-followup",
            "kind": "exogenous",
            "due_minute": 4,
            "priority": 0,
            "sequence": 12,
            "event_type": "information_correction",
            "payload": {
                "message_id": "claim-water-1",
                "zone_id": "sleeping",
                "belief": "rejected",
            },
            "source_id": "untrusted-staff-source",
            "causation_id": "rumor-event",
        }
    )
    scenario = ScenarioSpec.model_validate(document)
    world = run_scenario(scenario, tmp_path / "correction-outcomes", trace_level=TraceLevel.OFF)

    records = world.model_state[INFORMATION_SPREAD_STATE_KEY]["beliefs"]
    assert all(
        records[agent_id]["claim-water-1"]["correction_accepted"]
        for agent_id in ("A001", "A002")
    )
    assert all(
        not records[agent_id]["claim-water-1"]["last_correction_accepted"]
        for agent_id in ("A001", "A002")
    )
    correction_history = [
        item
        for item in world.model_state["body_state_v1"]["history"]
        if item["kind"] == "information_correction"
    ]
    assert correction_history
    assert all(item["event_id"] == "correction-event" for item in correction_history)
