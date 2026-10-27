"""Evidence, privacy and validation boundaries for post-run reviews, and Japanese demo
stories that never render model-authored claims.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import LLMStage, ProviderResponse
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.report import (
    build_narrative_card,
    build_review_evidence,
    generate_evidence_review,
    render_review_markdown,
    validate_comparison_scope,
)


class ReviewProvider:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.tasks = []

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        del max_output_tokens, timeout_seconds
        self.tasks.append(task)
        return ProviderResponse(
            text=json.dumps(self.response),
            model_id=model_id,
            input_tokens=30,
            output_tokens=20,
            elapsed_ms=3.0,
            request_id="review-test",
        )


def _evidence() -> Any:
    return build_review_evidence(
        metrics=[
            {
                "minute_index": 1440,
                "stress_mean": 0.7,
                "toilet_queue_length": 3,
                "source_ids_json": '{"private-agent-id":["agent-001"]}',
                "profile_name": "must-not-be-sent",
            }
        ],
        realized_events=[
            {
                "event": {
                    "event_id": "event-secret-42",
                    "event_type": "facility_outage",
                    "payload": {"agent_id": "agent-001", "notes": "private raw detail"},
                },
                "status": "applied",
                "realized_minute": 1440,
                "causation": "private causal text",
            }
        ],
        assumptions=["ASM-SIMPLE-PSYCHOLOGY-RATES-V1", "unknown-assumption"],
    )


def _router(response: dict[str, Any]) -> tuple[LLMRouter, ReviewProvider]:
    provider = ReviewProvider(response)
    router = LLMRouter(
        LLMSettings(
            mode=LLMMode.RECORD,
            api_key="test-key",
            max_input_tokens=4096,
            max_output_tokens=256,
            budget_usd=0.5,
        ),
        provider=provider,
        tape=ResponseTape(),
    )
    return router, provider


def test_review_sends_allowlisted_aggregates_and_renders_validated_evidence() -> None:
    response = {
        "observed": [{"metric_id": "stress_mean", "direction": "nonzero"}],
        "event_observations": [{"event_ref": "E001", "classification": "facility_outage"}],
        "assumptions": ["ASM-SIMPLE-PSYCHOLOGY-RATES-V1"],
        "hypotheses": [
            {
                "hypothesis": "event_may_explain_metric_change",
                "metric_ids": ["stress_mean"],
                "event_refs": ["E001"],
                "assumption_ids": ["ASM-SIMPLE-PSYCHOLOGY-RATES-V1"],
            }
        ],
        "recommendations": [],
    }
    router, provider = _router(response)

    result = generate_evidence_review(_evidence(), router)

    assert result.status == "validated"
    assert provider.tasks[0].stage is LLMStage.REVIEW
    assert provider.tasks[0].max_output_tokens == 2048
    schema = provider.tasks[0].output_schema
    assert schema["properties"]["observed"]["maxItems"] == 8
    assert schema["properties"]["event_observations"]["maxItems"] == 8
    assert schema["properties"]["hypotheses"]["maxItems"] == 3
    assert schema["properties"]["recommendations"]["maxItems"] == 3
    prompt = provider.tasks[0].input_text
    for private_value in (
        "event-secret-42",
        "agent-001",
        "must-not-be-sent",
        "private raw detail",
        "private causal text",
        "source_ids_json",
    ):
        assert private_value not in prompt
    assert '"event_ref":"E001"' in prompt
    assert '"stress_mean":0.7' in prompt

    rendered = render_review_markdown(result)
    assert "stress_mean" in rendered and "0.7" in rendered
    assert "event-secret-42" in rendered
    assert "event_may_explain_metric_change" not in rendered


def test_review_rejects_direction_claim_without_comparison_fact() -> None:
    response = {
        "observed": [{"metric_id": "stress_mean", "direction": "increase"}],
        "event_observations": [],
        "assumptions": [],
        "hypotheses": [],
        "recommendations": [],
    }
    router, _ = _router(response)

    result = generate_evidence_review(_evidence(), router)

    assert result.status == "rejected"
    assert "no paired comparison fact" in (result.validation_issue or "")
    rendered = render_review_markdown(result)
    assert "metric direction mismatch" not in rendered
    assert "changed increase" not in rendered


def test_schema_violation_falls_back_without_model_authored_text() -> None:
    response = {
        "observed": [{"metric_id": "invented_metric", "direction": "increase"}],
        "event_observations": [],
        "assumptions": [],
        "hypotheses": [],
        "recommendations": [],
    }
    router, _ = _router(response)

    result = generate_evidence_review(_evidence(), router)

    assert result.status == "rule_fallback"
    assert result.review["observed"] == []
    assert "invented_metric" not in render_review_markdown(result)


def test_comparison_scope_rejects_other_scenario() -> None:
    fixture = Path(__file__).parents[3] / "fixtures" / "scenario_100_agents.json"
    scenario = ScenarioSpec.model_validate_json(fixture.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="scenario ID"):
        validate_comparison_scope(scenario, {"scenario_id": "other"})


class StoryReviewProvider:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.tasks = []

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        del max_output_tokens, timeout_seconds
        self.tasks.append(task)
        return ProviderResponse(
            text=json.dumps(self.response),
            model_id=model_id,
            input_tokens=10,
            output_tokens=12,
            elapsed_ms=1,
            request_id="story-review-test",
        )


def _events() -> list[dict[str, Any]]:
    return [
        {
            "event": {
                "event_id": "water-stop-1",
                "event_type": "water_outage",
                "causation_id": None,
                "payload": {"agent_id": "private-agent-1", "secret": "never render"},
            },
            "realized_minute": 1500,
            "status": "applied",
            "detail": "private detail",
        },
        {
            "event": {
                "event_id": "water-return-1",
                "event_type": "water_restored",
                "causation_id": "water-stop-1",
                "payload": {"agent_id": "private-agent-1"},
            },
            "realized_minute": 1560,
            "status": "applied",
            "detail": "private detail",
        },
    ]


def _comparison() -> dict[str, Any]:
    return {
        "first_divergence_minute": 1500,
        "final_metric_differences": {
            "stress_mean": {"baseline": 0.8, "candidate": 0.5, "delta": -0.3},
            "toilet_queue_length": {"baseline": 2, "candidate": 0, "delta": -2},
            "source_ids_json": {
                "baseline": "private-agent-1",
                "candidate": "private-agent-1",
                "delta": 0,
            },
        },
    }


def test_offline_story_uses_only_recorded_event_and_kpi_facts() -> None:
    first = build_narrative_card(realized_events=_events(), comparison_report=_comparison())
    second = build_narrative_card(realized_events=_events(), comparison_report=_comparison())
    encoded = json.dumps(first.to_dict(), ensure_ascii=False)

    assert first == second
    assert first.mode == "offline"
    assert first.source_event_ids == ("water-stop-1", "water-return-1")
    assert first.metric_ids == ("toilet_queue_length", "stress_mean")
    assert "Day 2 01:00" in encoded
    assert "給水停止" in encoded and "給水再開" in encoded
    assert "water-stop-1` → `water-return-1" in encoded
    assert "因果関係の実証を意味しません" in encoded
    for private_value in ("private-agent-1", "never render", "private detail", "source_ids_json"):
        assert private_value not in encoded
    assert "stress_mean`）" in encoded
    assert "-0.3" in encoded


def test_opus_story_uses_validated_codes_and_allowlisted_prompt_only() -> None:
    response = {
        "observed": [{"metric_id": "stress_mean", "direction": "decrease"}],
        "event_observations": [{"event_ref": "E002", "classification": "water_restored"}],
        "assumptions": [],
        "hypotheses": [
            {
                "hypothesis": "event_may_explain_metric_change",
                "metric_ids": ["stress_mean"],
                "event_refs": ["E001"],
                "assumption_ids": [],
            }
        ],
        "recommendations": [],
    }
    provider = StoryReviewProvider(response)
    router = LLMRouter(
        LLMSettings(mode=LLMMode.RECORD, api_key="test-only", budget_usd=0.5),
        provider=provider,
        tape=ResponseTape(),
    )
    metrics = [{"minute_index": 1560, "stress_mean": 0.5}]
    evidence = build_review_evidence(
        metrics=metrics,
        realized_events=_events(),
        assumptions=[],
        comparison_report=_comparison(),
    )

    result = generate_evidence_review(evidence, router)
    card = build_narrative_card(
        realized_events=_events(), comparison_report=_comparison(), review_result=result
    )
    encoded = json.dumps(card.to_dict(), ensure_ascii=False)
    prompt = provider.tasks[0].input_text

    assert result.status == "validated"
    assert card.mode == "opus"
    assert "記録イベントはKPI差を調べる手掛かり" in encoded
    assert "`water-stop-1`" in encoded and "`stress_mean`" in encoded
    assert "model-authored" not in encoded
    assert "water-stop-1" not in prompt
    assert "private-agent-1" not in prompt
    assert "never render" not in prompt
    assert "private detail" not in prompt
    assert "source_ids_json" not in prompt


def test_opus_rejects_invented_event_reference_and_numeric_claim() -> None:
    response = {
        "observed": [{"metric_id": "stress_mean", "direction": "decrease", "reported_value": 99.0}],
        "event_observations": [{"event_ref": "E999", "classification": "water_outage"}],
        "assumptions": [],
        "hypotheses": [],
        "recommendations": [],
    }
    provider = StoryReviewProvider(response)
    router = LLMRouter(
        LLMSettings(mode=LLMMode.RECORD, api_key="test-only", budget_usd=0.25),
        provider=provider,
        tape=ResponseTape(),
    )
    evidence = build_review_evidence(
        metrics=[{"minute_index": 1560, "stress_mean": 0.5}],
        realized_events=_events(),
        assumptions=[],
        comparison_report=_comparison(),
    )

    result = generate_evidence_review(evidence, router)
    card = build_narrative_card(
        realized_events=_events(), comparison_report=_comparison(), review_result=result
    )

    assert result.status == "rule_fallback"
    assert card.mode == "offline"
    assert "E999" not in json.dumps(card.to_dict())
    assert "99" not in json.dumps(card.to_dict())


def test_empty_comparison_has_stable_no_op_card() -> None:
    first = build_narrative_card(realized_events=[], comparison_report={})
    second = build_narrative_card(realized_events=[], comparison_report={})

    assert first == second
    assert first.mode == "offline"
    assert first.observed_facts == ("表示できるイベント記録またはKPI差はありません。",)
    assert first.hypotheses == ()

