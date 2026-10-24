"""Offline-mocked integration for runtime routing and post-run review artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import _sanitize
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import ProviderResponse
from sheltersim.llm.provider import LLMRouter

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


class ReviewProvider:
    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        del max_output_tokens, timeout_seconds
        assert task.stage.value == "review"
        return ProviderResponse(
            text=json.dumps(
                {
                    "observed": [],
                    "event_observations": [],
                    "assumptions": [],
                    "hypotheses": [],
                    "recommendations": [],
                }
            ),
            model_id=model_id,
            input_tokens=24,
            output_tokens=9,
            elapsed_ms=2.0,
            request_id="mock-review-request",
        )


def test_api_enabled_run_writes_usage_and_separate_opus_review(tmp_path: Path) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["duration_minutes"] = 1
    document["scheduled_events"] = []
    scenario = ScenarioSpec.model_validate(document)
    settings = LLMSettings(
        mode=LLMMode.RECORD,
        api_key="mock-key",
        max_input_tokens=4096,
        max_output_tokens=64,
        budget_usd=0.1,
    )
    router = LLMRouter(settings, provider=ReviewProvider(), tape=ResponseTape())
    output = tmp_path / "run"

    world = run_scenario(scenario, output, llm_router=router)
    offline_world = run_scenario(scenario, tmp_path / "offline")

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    call_records = [
        json.loads(line)
        for line in (output / "llm_calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert manifest["execution_mode"] == "record"
    assert manifest["llm_usage"]["api_request_count"] == 1
    assert manifest["llm_usage"]["input_tokens"] == 24
    assert call_records[0]["model_id"] == settings.opus_model
    assert "# Evidence-bound Opus review" in (output / "review.md").read_text(encoding="utf-8")
    review = json.loads((output / "review.json").read_text(encoding="utf-8"))
    assert review["status"] == "validated"
    assert world.minute_index == 1
    assert world.canonical_json() == offline_world.canonical_json()


def test_artifact_sanitizer_preserves_usage_and_hides_secrets() -> None:
    sanitized = _sanitize(
        {
            "input_tokens": 24,
            "output_tokens": 3,
            "estimated_cost_usd": 0.0001,
            "access_token": "private",
            "api_key": "private",
        }
    )

    assert sanitized == {
        "input_tokens": 24,
        "output_tokens": 3,
        "estimated_cost_usd": 0.0001,
        "access_token": "[REDACTED]",
        "api_key": "[REDACTED]",
    }
