"""Opt-in, evidence-linked information diffusion and inquiry behavior in full Runs.

The first two tests run the shared generated Scenario, whose information policy
(evidence-aware-rumor-v1) is the one an operator's Scenario uses, with real
households. Until #137 they ran scenario_100_agents on the sparse policy, which the
UI does not install. The later tests keep fixtures
for the sparse policy's contract or name the UI's models explicitly.

The configuration, the transmission probability and the checks on saved state are
tested in tests/unit/policies/test_information.py.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TraceLevel
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.report import _ASSUMPTION_LABELS, scenario_assumption_ids

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "fixtures" / "scenario_100_agents.json"


@pytest.fixture
def generated_information_scenario(
    generated_previews, information_spread_scenario, information_spread_config
) -> Callable[..., ScenarioSpec]:
    """The shared generated Scenario with information_spread_scenario's rumor and correction.

    The generated information config keeps its policy and sources, with the test
    coefficients over it so every contact passes a claim on. Residents have no needs,
    so information is the only thing that drives inquiries. place moves people to
    other areas at the start.
    """

    def build(
        *,
        duration: int,
        correction: bool = True,
        origin: str = "A001",
        place: dict[str, str] | None = None,
    ) -> ScenarioSpec:
        document = copy.deepcopy(generated_previews()["scenario"])
        events = information_spread_scenario(duration=duration, correction=correction)
        document["duration_minutes"] = duration
        document["scheduled_events"] = events.model_dump(mode="json")["scheduled_events"]
        document["scheduled_events"][0]["payload"]["origin_agent_id"] = origin
        configs = document["model_configs"]
        configs["information_spread"] = configs["information_spread"] | information_spread_config()
        configs["behavior"]["temperature"] = 0.01
        for profile in document["profiles"]:
            profile["support_need"] = 0.0
            profile["initial_needs"] = {"toilet_urgency": 0.0, "hunger": 0.0, "fatigue": 0.0}
            if profile["id"] in (place or {}):
                profile["initial_zone_id"] = place[profile["id"]]
        return ScenarioSpec.model_validate(document)

    return build


def test_rumor_spreads_only_through_local_contacts_and_correction_resolves_truth(
    tmp_path: Path, generated_information_scenario
) -> None:
    # A003 starts the rumor in the sleeping area. A002 lives alone and stands in the
    # corridor next to it; A004 is A003's household member in the toilet area, which
    # is not next to the sleeping area; A013 lives alone there. Being family changes
    # only how likely a contact passes a claim on, not who is in contact.
    scenario = generated_information_scenario(
        duration=2,
        correction=False,
        origin="A003",
        place={"A002": "corridor", "A004": "toilet", "A013": "toilet"},
    )
    households = {profile.id: profile.group_id for profile in scenario.profiles}
    assert households["A004"] == households["A003"]
    for agent_id in ("A002", "A013"):
        assert list(households.values()).count(households[agent_id]) == 1

    world = run_scenario(scenario, tmp_path / "full", trace_level=TraceLevel.FULL)
    info = world.model_state[INFORMATION_SPREAD_STATE_KEY]

    assert info["messages"]["claim-water-1"]["truth_status"] == "unknown"
    assert info["counters"]["contact_deliveries"] >= 1
    traces = [
        json.loads(line)
        for line in (tmp_path / "full" / "information_contacts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    first_contacts = [
        contact for contact in traces[0]["contacts"] if contact["sender_agent_id"] == "A003"
    ]
    assert {contact["contact_basis"] for contact in first_contacts} <= {
        "same_zone",
        "adjacent_zone",
        "family",
    }
    recipients = {contact["recipient_agent_id"]: contact for contact in first_contacts}
    assert "A004" not in recipients
    assert "A013" not in recipients
    assert "claim-water-1" not in info["beliefs"]["A004"]
    assert "claim-water-1" not in info["beliefs"]["A013"]
    assert info["beliefs"]["A002"]["claim-water-1"]["belief"] == "heard"
    adjacent_contact = recipients["A002"]
    assert adjacent_contact["contact_basis"] == "adjacent_zone"
    assert adjacent_contact["transmission_selected"]
    assert adjacent_contact["message_source_id"] == "scenario-rumor-source"
    assert adjacent_contact["causation_id"] == "rumor-event"

    correction_scenario = generated_information_scenario(duration=6)
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
        and record.event.payload["recipient_agent_id"] != "A001"
    )
    assert contact.status.value == "applied"
    assert contact.event.source_id == "scenario-rumor-source"
    assert realized["correction-event"].event.source_id == "staff-notice-source"
    assert realized["correction-event"].event.causation_id == "rumor-event"
    corrected_origin = corrected_info["beliefs"]["A001"]["claim-water-1"]
    assert corrected_origin["source"] == "staff-notice-source"
    assert corrected_origin["last_event_id"] == "correction-event"

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
    information = manifest["model_assumptions"]["information_spread"]
    assert information["source_id"] == "ASM-INFORMATION-SPREAD-V1"
    assert information["policy_version"] == "evidence-aware-rumor-v1"
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
    tmp_path: Path, generated_information_scenario
) -> None:
    scenario = generated_information_scenario(duration=6, correction=False)
    traced = run_scenario(scenario, tmp_path / "traced", trace_level=TraceLevel.FULL)
    untraced = run_scenario(scenario, tmp_path / "untraced", trace_level=TraceLevel.OFF)
    assert traced.canonical_json() == untraced.canonical_json()

    # A generated Scenario always configures information_spread, so the default
    # stays a fixture Scenario without the setting.
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


def test_false_rumor_is_rejected_by_a_causal_correction(
    tmp_path: Path, information_spread_scenario
) -> None:
    world = run_scenario(
        information_spread_scenario(duration=4, truth_status="false"),
        tmp_path / "false-rumor",
        trace_level=TraceLevel.OFF,
    )
    info = world.model_state[INFORMATION_SPREAD_STATE_KEY]

    assert info["messages"]["claim-water-1"]["truth_status"] == "false"
    assert all(
        beliefs["claim-water-1"]["belief"] == "rejected" for beliefs in info["beliefs"].values()
    )


def test_false_rumor_flows_through_integrated_psychology_trace(
    tmp_path: Path, information_spread_scenario
) -> None:
    document = information_spread_scenario(
        duration=3, correction=False, truth_status="false"
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
    tmp_path: Path, information_spread_config, information_spread_scenario
) -> None:
    traced_scenarios: dict[str, dict[int, dict[str, object]]] = {}
    corrected_world = None
    for name, correction in (("rumor-only", False), ("rumor-corrected", True)):
        document = information_spread_scenario(
            duration=5,
            correction=correction,
            truth_status="false",
            config=information_spread_config(
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
    tmp_path: Path, information_spread_config, information_spread_scenario
) -> None:
    outcomes: dict[str, dict[int, dict[str, object]]] = {}
    refused_world = None
    for name, correction in (("rumor-only", False), ("rumor-refused", True)):
        document = information_spread_scenario(
            duration=5,
            correction=correction,
            truth_status="false",
            config=information_spread_config(
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
    assert outcomes["rumor-refused"][3]["stress"]["after"] == pytest.approx(
        outcomes["rumor-only"][3]["stress"]["after"]
    )


def test_evidence_aware_correction_tracks_exposure_and_probabilistic_acceptance(
    tmp_path: Path, information_spread_config, information_spread_scenario
) -> None:
    scenario = information_spread_scenario(
        duration=6,
        profile_count=100,
        truth_status="false",
        config=information_spread_config(
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
    tmp_path: Path, information_spread_config, information_spread_scenario
) -> None:
    scenario = information_spread_scenario(
        duration=7,
        profile_count=2,
        truth_status="false",
        config=information_spread_config(
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
        records[agent_id]["claim-water-1"]["correction_accepted"] for agent_id in ("A001", "A002")
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
