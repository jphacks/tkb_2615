"""The PDF report: a saved Run to report on, grounded prose and its fake Opus router."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def report_saved_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved_timeline: Callable[..., Any]
) -> Iterator[Any]:
    """A ProjectAPI over Run r of 12 people for 40 minutes, checkpointed every 10 minutes,
    with a no-op fork and a fork at minute 10 that adds a toilet of capacity 3.
    """
    from sheltersim.experiments.branch import AddToiletCommand
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.storage.projects import digest
    from sheltersim.web.projects import ProjectAPI

    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    source = json.loads(ScenarioSpec.load("fixtures/scenario_100_agents.json").canonical_json())
    source["duration_minutes"] = 40
    source["profiles"] = source["profiles"][:12]
    source["scheduled_events"] = []
    source["model_configs"]["toilet_service"] = {}
    scenario = ScenarioSpec.model_validate(source)
    store, timeline = saved_timeline(tmp_path / "projects", scenario)
    timeline.advance("main", 40, checkpoint_interval=10)
    timeline.fork("main", 10, "noop")
    timeline.advance("noop", 40)
    source_runtime = timeline.seek("main", 10)
    child = source_runtime.fork("capacity")
    child.runtime_state["fork_origin"] = {
        "branch_id": "main",
        "minute": 10,
        "state_hash": digest(source_runtime.world.to_dict()),
        "checkpoint_hash": source_runtime.checkpoint().checkpoint_hash,
    }
    child.apply(AddToiletCommand("extra", "toilet", capacity=3))
    timeline.record(child, fork_minute=10)
    timeline.advance("capacity", 40)
    api = ProjectAPI(store)
    try:
        yield api
    finally:
        api.close()


def _prose_document() -> dict[str, Any]:
    return {
        "executive": [
            {
                "text": "待機と支援需要を合わせて確認することで、"
                "運営上の着眼点を整理できる可能性があります。",
                "refs": ["B0.toilet_queue_length"],
            }
        ],
        "interpretation": [
            {
                "text": "待機の継続には処理能力だけでなく、"
                "需要の集中が関係している可能性があります。",
                "refs": ["B0.window.toilet_queue_length"],
            }
        ],
        "hypotheses": [
            {
                "text": "待機と支援需要の重なりが運営上の負担につながる可能性があります。",
                "refs": ["B0.unresolved_support_request_count", "B0.toilet_queue_length"],
            }
        ],
        "proposals": [
            {
                "text": "優先的に、需要の時間帯と設備容量をそろえた比較条件で、"
                "追加検証することを検討してください。",
                "refs": ["B0.toilet_queue_length"],
            },
            {
                "text": "代替案として、担当者の配置条件を変えて、"
                "待機と支援の推移を比較することを検討してください。",
                "refs": ["B0.unresolved_support_request_count"],
            },
        ],
        "limitations": [
            {
                "text": "現地の集計データとの照合と、異なる条件での反復検証が必要です。",
                "refs": ["L"],
            }
        ],
    }


@pytest.fixture
def prose_document() -> Callable[[], dict[str, Any]]:
    """A grounded report analysis that cites only allow-listed evidence."""
    return _prose_document


class _ReportProvider:
    def __init__(self, *, error=False):
        self.calls = 0
        self.error = error
        self.prompts = []

    def complete(self, *, model_id, task, **_kwargs):
        from sheltersim.llm.contracts import LLMProviderError, ProviderResponse

        self.calls += 1
        self.prompts.append(task.input_text)
        if self.error:
            raise LLMProviderError("Anthropic API request failed; HTTP 429", status_code=429)
        return ProviderResponse(
            json.dumps(_prose_document(), ensure_ascii=False), model_id, 200, 500, 1
        )


@pytest.fixture
def report_provider() -> type[_ReportProvider]:
    """Make a fake Opus provider that answers prose_document, or fails with HTTP 429."""
    return _ReportProvider


def _report_router(provider=None, **kwargs):
    from sheltersim.llm.cache import ResponseTape
    from sheltersim.llm.config import LLMMode, LLMSettings
    from sheltersim.llm.provider import LLMRouter

    settings = LLMSettings(mode=LLMMode.RECORD, api_key="test-only", **kwargs)
    return LLMRouter(settings, provider=provider or _ReportProvider(), tape=ResponseTape())


@pytest.fixture
def report_router() -> Callable[..., Any]:
    """Make a recording LLMRouter for report analysis (report_provider by default)."""
    return _report_router


def _install_report_router(api, monkeypatch, provider=None):
    router = _report_router(provider)
    monkeypatch.setattr(api.workspace, "analysis_router", lambda *_a, **_kw: router)
    return router


@pytest.fixture
def install_report_router() -> Callable[..., Any]:
    """Make a Project workspace use report_router for its analysis; return the router."""
    return _install_report_router
