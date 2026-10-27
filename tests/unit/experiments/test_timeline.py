"""Replaying recorded LLM answers never asks the provider or writes the shared response tape."""

from __future__ import annotations

from pathlib import Path

import pytest

from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMSettings
from sheltersim.llm.contracts import LLMStage, LLMTask, ProviderResponse
from sheltersim.llm.provider import LLMRouter


def test_historical_router_cannot_backfill_or_write_global_tape(
    counting_provider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sheltersim.experiments.timeline import (
        RecordedRouter,
        TimelineError,
        public_router_settings,
    )

    monkeypatch.chdir(tmp_path)
    settings = LLMSettings(mode="record", api_key="unused", budget_usd=1)
    task = LLMTask(
        stage=LLMStage.RUNTIME,
        input_text="recorded input",
        state_hash="state",
        prompt_version="v1",
        fallback_text='{"ok":false}',
        max_output_tokens=100,
    )
    live = LLMRouter(settings, provider=counting_provider, tape=ResponseTape())
    start = live.snapshot_state()
    live.complete(task)
    complete = live.snapshot_state()
    complete["response_tape"] = {"schema_version": 1, "entries": {}}
    global_path = tmp_path / ".sheltersim/llm-tape.json"
    tape = ResponseTape(path=global_path)
    tape.put(
        task.cache_key(settings.model_for(task.stage.value)),
        ProviderResponse(
            text='{"ok":true}', model_id="fake", input_tokens=1, output_tokens=1, elapsed_ms=0
        ),
    )
    original = global_path.read_bytes()
    recorded = public_router_settings(live)
    assert "api_key" not in recorded
    assert recorded["settings_schema_version"] == 3
    replay = RecordedRouter(start, complete, recorded)
    with pytest.raises(TimelineError, match="missing accepted response"):
        replay.complete(task)
    assert global_path.read_bytes() == original
