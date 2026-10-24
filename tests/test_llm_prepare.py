"""Synthetic ScenarioSpec preparation through an offline-testable provider boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings, ModelPrice
from sheltersim.llm.contracts import LLMStage, ProviderResponse
from sheltersim.llm.prepare import generate_scenario_draft
from sheltersim.llm.provider import LLMRouter

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


class FakeProvider:
    def __init__(self, response: str, *, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        self.calls.append((model_id, task.input_text))
        if self.error is not None:
            raise self.error
        return ProviderResponse(
            text=self.response,
            model_id=model_id,
            input_tokens=80,
            output_tokens=100,
            elapsed_ms=9.0,
        )


def tiny_scenario() -> ScenarioSpec:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["id"] = "prepare-fixture"
    document["duration_minutes"] = 20
    document["profiles"] = document["profiles"][:2]
    document["scheduled_events"] = [
        {
            "id": "existing-staff-absence",
            "kind": "exogenous",
            "due_minute": 2,
            "priority": 0,
            "sequence": 0,
            "event_type": "staff_unavailable",
            "payload": {
                "staff_id": document["staff"][0]["id"],
                "until_minute": 5,
            },
            "source_id": "qa-assumption",
        }
    ]
    document["model_configs"] = {}
    return ScenarioSpec.model_validate(document)


def response_for(scenario: ScenarioSpec) -> str:
    return json.dumps(
        {
            "profiles": [
                {
                    "profile_index": index,
                    "family_index": 0,
                    "zone_index": 2,
                    "mobility": "assisted" if index == 0 else "independent",
                    "support_need": 0.2 + index / 10,
                    "stress_sensitivity": 0.4,
                    "initial_needs": {
                        "toilet_urgency": 0.1,
                        "hunger": 0.3,
                        "fatigue": 0.5,
                    },
                }
                for index, _ in enumerate(scenario.profiles)
            ],
            "events": [
                {
                    "event_type": "facility_outage",
                    "due_minute": 10,
                    "facility_index": 0,
                    "units": 1,
                },
                {
                    "event_type": "facility_recovery",
                    "due_minute": 15,
                    "facility_index": 0,
                    "units": 1,
                },
            ],
        }
    )


def make_router(response: str, *, mode: LLMMode = LLMMode.RECORD, tape=None) -> LLMRouter:
    settings = LLMSettings(
        mode=mode,
        api_key="test-key" if mode is LLMMode.RECORD else None,
        max_api_calls=2,
        max_input_tokens=4096,
        max_output_tokens=8192,
        budget_usd=1.0,
        opus_price=ModelPrice(1, 2),
        haiku_price=ModelPrice(0.1, 0.5),
    )
    return LLMRouter(settings, provider=FakeProvider(response), tape=tape or ResponseTape())


def test_scenario_generation_creates_valid_synthetic_profiles_and_events() -> None:
    scenario = tiny_scenario()
    router = make_router(response_for(scenario), tape=ResponseTape())

    draft, manifest = generate_scenario_draft(scenario, router)

    assert router.records[0].stage == LLMStage.PREPARE.value
    assert router.records[0].model_id == router.settings.opus_model
    assert len(draft.profiles) == len(scenario.profiles) == 2
    assert {profile.group_id for profile in draft.profiles} == {"G001"}
    assert {profile.initial_zone_id for profile in draft.profiles} == {"sleeping"}
    assert draft.profiles[0].provenance.source_kind == "llm"
    assert [event.due_minute for event in draft.scheduled_events] == [2, 10, 15]
    assert draft.scheduled_events[0].id == "existing-staff-absence"
    assert draft.scheduled_events[0].sequence == 0
    assert draft.scheduled_events[1].payload == {"facility_id": "toilet_1", "units_out": 1}
    assert draft.scheduled_events[1].sequence == 1
    assert draft.scheduled_events[2].payload == {
        "facility_id": "toilet_1",
        "units_restored": 1,
    }
    assert draft.scheduled_events[2].sequence == 2
    assert manifest["status"] == "generated"
    assert manifest["generated_scenario_hash"]
    assert "A001" not in router.provider.calls[0][1]
    assert "sleeping" not in router.provider.calls[0][1]


def test_scenario_generation_replay_reuses_output_and_is_state_identical() -> None:
    scenario = tiny_scenario()
    tape = ResponseTape()
    recorded, _ = generate_scenario_draft(scenario, make_router(response_for(scenario), tape=tape))
    replay_router = make_router("{}", mode=LLMMode.REPLAY, tape=tape)

    replayed, manifest = generate_scenario_draft(scenario, replay_router)

    assert replayed.canonical_json() == recorded.canonical_json()
    assert replay_router.api_request_count == 0
    assert replay_router.records[0].cache_hit is True
    assert manifest["cache_hit"] is True


def test_malformed_or_pii_shaped_output_falls_back_without_mutating_scenario() -> None:
    scenario = tiny_scenario()
    poisoned = json.dumps(
        {
            "profiles": [],
            "events": [],
            "person_name": "Someone",
        }
    )
    router = make_router(poisoned, tape=ResponseTape())

    draft, manifest = generate_scenario_draft(scenario, router)

    assert draft.canonical_json() == scenario.canonical_json()
    assert manifest["status"] == "rule_fallback"
    assert router.records[0].fallback_used is True
    assert router.records[0].outcome == "invalid_output"


def test_duplicate_profile_slots_and_impossible_event_capacity_are_rejected() -> None:
    scenario = tiny_scenario()
    duplicate = json.loads(response_for(scenario))
    duplicate["profiles"][1]["profile_index"] = 0
    router = make_router(json.dumps(duplicate), tape=ResponseTape())
    with pytest.raises(ValueError, match="each profile slot exactly once"):
        generate_scenario_draft(scenario, router)

    impossible = json.loads(response_for(scenario))
    impossible["events"] = [
        {
            "event_type": "facility_outage",
            "due_minute": 10,
            "facility_index": 0,
            "units": 1,
        },
        {
            "event_type": "facility_outage",
            "due_minute": 11,
            "facility_index": 0,
            "units": 1,
        },
    ]
    router = make_router(json.dumps(impossible), tape=ResponseTape())
    with pytest.raises(ValueError, match="exceeds available working units"):
        generate_scenario_draft(scenario, router)
