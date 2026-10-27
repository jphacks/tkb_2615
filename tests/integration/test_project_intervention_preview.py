"""A Project's intervention preview sends a running generated Scenario to Opus.

The compiler's own contract (repair, fallback, redaction, stop policy) is
checked in tests/unit/llm/test_natural_language.py.
"""

from __future__ import annotations

import json

import pytest

from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMErrorPolicy, LLMMode, LLMSettings, ModelPrice
from sheltersim.llm.contracts import LLMProviderError, ProviderResponse
from sheltersim.llm.provider import LLMRouter
from sheltersim.storage.projects import ProjectStore, StorageError, digest
from sheltersim.web import workspace
from sheltersim.web.projects import ProjectAPI


class FakeProvider:
    def __init__(self, *responses: str, error: Exception | None = None) -> None:
        self.responses = list(responses)
        self.error = error
        self.calls = []

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        self.calls.append((model_id, task))
        if self.error is not None:
            raise self.error
        return ProviderResponse(
            text=self.responses.pop(0),
            model_id=model_id,
            input_tokens=120,
            output_tokens=80,
            elapsed_ms=12.0,
        )


def _router(provider: FakeProvider, *, stop: bool = False) -> LLMRouter:
    return LLMRouter(
        LLMSettings(
            mode=LLMMode.RECORD,
            api_key="test-key",
            max_api_calls=2,
            max_input_tokens=16_384,
            max_output_tokens=4096,
            budget_usd=1.0,
            opus_price=ModelPrice(1, 2),
            haiku_price=ModelPrice(0.1, 0.5),
            error_policy=LLMErrorPolicy.STOP if stop else LLMErrorPolicy.FALLBACK,
        ),
        provider=provider,
        tape=ResponseTape(),
    )


def _mid_run_project(tmp_path, scenario, minute: int) -> tuple[ProjectStore, str]:
    """A Project store whose generated Scenario's main Branch reached `minute`."""
    store = ProjectStore(tmp_path)
    store.create_project("project")
    saved = store.save_scenario(
        "project", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
    )
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    timeline = TimelineStore(store, "project", "run")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    timeline.advance("main", minute)
    return store, sorted(timeline.seek("main", minute).world.zones)[0]


def test_generated_scenario_mid_run_intervention_reaches_opus(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    store, zone_id = _mid_run_project(tmp_path, generated_scenario, 60)
    provider = FakeProvider(
        json.dumps(
            {
                "status": "compiled",
                "support_mode": "native",
                "mechanism_summary": "支援要員を追加します。",
                "effects": [
                    {
                        "op": "add_staff",
                        "target_kind": "zone",
                        "target_id": zone_id,
                        "start_minute": 60,
                        "parameters": {"count": 2},
                        "assumption_indexes": [],
                    }
                ],
                "assumptions": [],
                "limitations": [],
                "questions": [],
                "choices": [],
            },
            ensure_ascii=False,
        )
    )
    # The default generic guard is 16,384 bytes; the generated context is larger.
    monkeypatch.setattr(workspace, "accounted_router", lambda *_a, **_k: _router(provider))
    api = ProjectAPI(store, recover=False)
    try:
        result = api.post(
            ["project", "runs", "run", "branches", "main", "intervention-preview"],
            {"minute": 60, "text": "スタッフを2人追加"},
        )
    finally:
        api.close()

    assert len(provider.calls) == 1
    assert len(provider.calls[0][1].input_text.encode()) > 16_384
    assert result["status"] == "compiled"
    assert result["fallback_used"] is False
    assert result["usage"]["api_request_count"] == 1
    assert result["can_apply"] is True
    assert [effect["start_minute"] for effect in result["program"]["effects"]] == [60]


def test_project_preview_returns_the_stop_reason_as_a_conflict(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    store, _ = _mid_run_project(tmp_path, generated_scenario, 30)
    provider = FakeProvider(error=LLMProviderError("rate limited", status_code=429))
    monkeypatch.setattr(
        workspace, "accounted_router", lambda *_a, **_k: _router(provider, stop=True)
    )
    api = ProjectAPI(store, recover=False)
    try:
        with pytest.raises(StorageError) as raised:
            api.post(
                ["project", "runs", "run", "branches", "main", "intervention-preview"],
                {"minute": 30, "text": "スタッフを2人追加"},
            )
    finally:
        api.close()

    assert raised.value.code == "intervention_compiler_unavailable"
    assert "429" in str(raised.value)
