"""Fake LLM providers and the gated live API checks."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


class _CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, **_kwargs: object) -> Any:
        from sheltersim.llm.contracts import ProviderResponse

        self.calls += 1
        return ProviderResponse(
            text='{"ok":true}', model_id="fake", input_tokens=5, output_tokens=3, elapsed_ms=0
        )


@pytest.fixture
def counting_provider() -> _CountingProvider:
    """A fake LLM provider that answers {"ok":true} and counts its calls."""
    return _CountingProvider()


_LIVE_ACK_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _live_model_check(settings: Any, cache_path: Path, provider: Any = None) -> tuple:
    import uuid
    from dataclasses import replace

    from sheltersim.llm.accounting import AccountedProvider, runtime_usage_store, usage_limits
    from sheltersim.llm.contracts import LLMStage, LLMTask
    from sheltersim.llm.provider import LLMRouter
    from sheltersim.storage.usage import UsageLedger, task_campaign_ledger

    # Opus and Sonnet always think, so each non-runtime call reserves thinking headroom
    # (about $0.09 for Opus before the call); actual spend stays far below this cap.
    settings = replace(
        settings.for_live_integration_tests(),
        max_api_calls=3,
        live_test_max_calls=3,
        live_test_budget_usd=0.15,
        budget_usd=0.15,
        intervention_model_family="sonnet",
        cache_path=cache_path,
    )
    store = runtime_usage_store()
    store.create_project("runtime-accounts", _internal=True)
    # Each run gets its own 3-call account. One fixed account kept every earlier run's
    # receipts, so the caps covered the machine's lifetime and a second run could not pass
    # (#148). The machine-wide task campaign ledger still bounds all runs together.
    ledger = UsageLedger(store, "runtime-accounts", f"live-task-qa-{uuid.uuid4().hex}")
    ledger.create(usage_limits(settings))
    router = LLMRouter(settings)
    router.provider = AccountedProvider(
        provider or router.provider,
        ledger,
        settings,
        "live_api_qa",
        branch_id=None,
        campaign_ledger=task_campaign_ledger(store),
    )
    # A runtime call must leave calls for the later analysis (ANALYSIS_RESERVED_CALLS), so
    # on a 3-call account it fits only before the other two (#148).
    tasks = [
        LLMTask(
            stage=stage,
            input_text='Reply with exactly {"ok":true}.',
            state_hash=f"live-test-{stage.value}",
            prompt_version="live-integration-ack-v1",
            output_schema=_LIVE_ACK_SCHEMA,
            fallback_text='{"ok":false}',
            max_output_tokens=32,
        )
        for stage in (LLMStage.RUNTIME, LLMStage.PREPARE, LLMStage.INTERVENTION)
    ]
    return settings, router, tasks, store


def _live_source_search_check(settings: Any, cache_path: Path, provider: Any = None) -> tuple:
    import uuid
    from dataclasses import replace

    from sheltersim.llm.accounting import AccountedProvider, runtime_usage_store, usage_limits
    from sheltersim.llm.provider import LLMRouter
    from sheltersim.region.models import RegionResearchRequest
    from sheltersim.region.research import _official_source_search_task
    from sheltersim.storage.usage import UsageLedger, task_campaign_ledger

    # Two calls of allowance: a web-search call reserves output for two sampling passes.
    settings = replace(
        settings,
        mode="record",
        max_api_calls=2,
        max_input_tokens=4096,
        max_output_tokens=256,
        max_concurrency=1,
        timeout_seconds=120,
        budget_usd=10,
        cache_path=cache_path,
    )
    store = runtime_usage_store()
    store.create_project("runtime-accounts", _internal=True)
    # Each run gets its own account, as in _live_model_check. Every run sends the same
    # request with a fresh tape, and an account refuses a request key it already holds a
    # receipt for (usage_receipt_exists), so a fixed account let only the first run pass
    # (#152). Its receipts would also make the 2-call cap cover every run on the machine.
    local = UsageLedger(
        store, "runtime-accounts", f"live-source-search-stream-qa-{uuid.uuid4().hex}"
    )
    local.create(usage_limits(settings))
    campaign = task_campaign_ledger(store)
    router = LLMRouter(settings)
    router.provider = AccountedProvider(
        provider or router.provider,
        local,
        settings,
        "live_official_source_search",
        branch_id=None,
        campaign_ledger=campaign,
    )
    task = _official_source_search_task(
        RegionResearchRequest(
            municipality_query="墨田区", selected_municipality_name="墨田区", disaster="earthquake"
        )
    )
    return settings, router, task, campaign


@pytest.fixture
def live_source_search_check() -> Callable[..., tuple]:
    """Build the live official-domain search check: settings, accounted router, task.

    Call it like live_model_check. It returns (settings, router, task, campaign ledger).
    tests/live/test_live_api.py runs it against the real API; tests/unit/llm/
    test_accounting.py checks offline that two runs each fit their own allowance.
    """
    return _live_source_search_check


@pytest.fixture
def live_model_check() -> Callable[..., tuple]:
    """Build the live model check: its capped settings, an accounted router and its tasks.

    Call it with LLMSettings, the tape path and, offline, a fake provider. It returns
    (settings, router, tasks, usage store). tests/live/test_live_api.py runs it against the
    real API; tests/unit/llm/test_accounting.py checks offline that it fits the ledgers.
    """
    return _live_model_check
