"""Evidence, privacy and validation boundaries for post-run reviews."""

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
