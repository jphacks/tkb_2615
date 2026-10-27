"""LLM call outcomes are summarized so that rule fallbacks are never reported as clean,
and a scenario text parse that falls back to rules warns about it.

Runs that surface these outcomes are checked in tests/integration/test_llm_outcomes.py.
"""

from __future__ import annotations

from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import ProviderResponse
from sheltersim.llm.outcomes import fallback_warning, summarize_call_outcomes
from sheltersim.llm.provider import LLMRouter


class TruncatedProvider:
    """Reply with cut-off JSON, as a response stopped at max_tokens would."""

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        del max_output_tokens, timeout_seconds
        return ProviderResponse(
            text='{"profiles":[{"profile_index":0,',
            model_id=model_id,
            input_tokens=10,
            output_tokens=5,
            elapsed_ms=0.0,
        )


def _router(provider, *, max_api_calls: int = 1_000) -> LLMRouter:
    settings = LLMSettings(
        mode=LLMMode.RECORD, api_key="mock-key", max_api_calls=max_api_calls, budget_usd=1.0
    )
    return LLMRouter(settings, provider=provider, tape=ResponseTape())


def _record(
    index: int,
    outcome: str,
    *,
    fallback: bool,
    error_kind: str | None = None,
    prompt_version: str = "support-priority-v1",
) -> dict[str, object]:
    return {
        "call_index": index,
        "outcome": outcome,
        "fallback_used": fallback,
        "error_kind": error_kind,
        "prompt_version": prompt_version,
    }


def test_summary_classifies_every_status() -> None:
    assert summarize_call_outcomes([])["status"] == "not_requested"
    offline = [_record(0, "offline", fallback=True)]
    assert summarize_call_outcomes(offline)["status"] == "offline_rules"
    assert (
        summarize_call_outcomes(
            [_record(0, "recorded", fallback=False), _record(1, "cache_hit", fallback=False)]
        )["status"]
        == "all_accepted"
    )
    assert (
        summarize_call_outcomes([_record(0, "shared_allowance_unavailable", fallback=True)])[
            "status"
        ]
        == "all_fallback"
    )


def test_summary_counts_caps_and_validation_failures_as_fallbacks() -> None:
    summary = summarize_call_outcomes(
        [
            _record(0, "invalid_output", fallback=True, error_kind="schema_validation_failed"),
            _record(1, "recorded", fallback=False),
            _record(
                2,
                "max_api_calls",
                fallback=True,
                error_kind="max_api_calls",
                prompt_version="event-triggered-social-v2",
            ),
            _record(3, "max_api_calls", fallback=True, error_kind="max_api_calls"),
        ]
    )

    assert summary["status"] == "degraded"
    assert summary["accepted_count"] == 1
    assert summary["fallback_count"] == 3
    assert summary["fallback_by_reason"] == {"invalid_output": 1, "max_api_calls": 2}
    assert summary["fallback_by_error_kind"] == {
        "max_api_calls": 2,
        "schema_validation_failed": 1,
    }
    assert summary["fallback_by_prompt_version"] == {
        "event-triggered-social-v2": 1,
        "support-priority-v1": 2,
    }
    assert summary["first_fallback_call_index"] == 0
    warning = fallback_warning(summary)
    assert warning is not None and "3 of 4" in warning and "max_api_calls: 2" in warning
    clean = summarize_call_outcomes([_record(0, "recorded", fallback=False)])
    assert fallback_warning(clean) is None


def test_scenario_text_parse_warns_on_rule_fallback() -> None:
    from sheltersim.llm.region_prepare import parse_scenario_text

    parsed = parse_scenario_text(
        "横浜市で地震が発生。避難者30人、期間は1日。", router=_router(TruncatedProvider())
    )

    assert parsed.source == "rules_fallback"
    assert "AIの入力整理を利用できず、明示された値のみを使用します。" in parsed.warnings
    assert parsed.request is not None  # explicitly stated values remain usable
