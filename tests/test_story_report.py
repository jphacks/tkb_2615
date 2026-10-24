"""Evidence-backed Japanese demo stories never render model-authored claims."""

from __future__ import annotations

import json
from typing import Any

from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import ProviderResponse
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.report import (
    build_narrative_card,
    build_review_evidence,
    generate_evidence_review,
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
    provider = ReviewProvider(response)
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
    provider = ReviewProvider(response)
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
