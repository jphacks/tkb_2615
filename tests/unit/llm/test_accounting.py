"""Paid LLM calls are reserved on the usage ledgers before the provider is asked.

AccountedProvider reserves on the Project's ledger and on the machine-wide task
campaign ledger, settles what the provider reports, and falls back without a
call when either limit would be passed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sheltersim.contracts.workspace import UsageLimits
from sheltersim.llm.accounting import AccountedProvider
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMSettings
from sheltersim.llm.contracts import (
    LLMProviderError,
    LLMStage,
    LLMTask,
    ProviderResponse,
)
from sheltersim.llm.provider import LLMRouter
from sheltersim.storage.projects import ProjectStore, StorageError
from sheltersim.storage.usage import UsageLedger, task_campaign_ledger


def test_sibling_routers_share_paid_cap_and_denial_costs_zero(
    project_store_at, counting_provider, tmp_path: Path
) -> None:
    store = project_store_at(tmp_path)
    ledger = UsageLedger(store, "project", "account")
    ledger.create(
        UsageLimits(max_calls=1, max_input_tokens=20000, max_output_tokens=1000, budget_usd=1)
    )
    provider = counting_provider
    settings = LLMSettings(
        mode="record",
        api_key="unused",
        max_api_calls=10,
        budget_usd=1,
        cache_path=tmp_path / "unused.json",
    )
    task = LLMTask(
        stage=LLMStage.RUNTIME,
        input_text="synthetic evidence",
        state_hash="state",
        prompt_version="contract-v1",
        fallback_text='{"ok":false}',
        max_output_tokens=100,
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )
    routers = [
        LLMRouter(
            settings,
            provider=AccountedProvider(provider, ledger, settings, "runtime", branch),
            tape=ResponseTape(),
        )
        for branch in ("main", "child")
    ]
    assert routers[0].complete(task) == '{"ok":true}'
    assert routers[1].complete(task) == '{"ok":false}'
    assert provider.calls == 1
    assert ledger.summary()["totals"]["calls"] == 1
    assert routers[1].records[-1].api_request_count == 0
    assert routers[1].records[-1].estimated_cost_usd == 0


def test_task_campaign_cap_is_shared_across_accounts_and_process_reloads(
    project_store_at,
    counting_provider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "global-state"))
    store_a = project_store_at(tmp_path / "project-a")
    store_b = project_store_at(tmp_path / "project-b")
    campaign_a = task_campaign_ledger(store_a)
    campaign_a.reserve(
        request_key="held-near-cap",
        operation="qa",
        branch_id="main",
        stage="prepare",
        model_id="fake",
        input_tokens=1,
        output_tokens=1,
        cost_usd=49.999,
    )

    local_b = UsageLedger(store_b, "project", "account-b")
    local_b.create(
        UsageLimits(max_calls=2, max_input_tokens=20000, max_output_tokens=1000, budget_usd=1)
    )
    provider = counting_provider
    settings = LLMSettings(
        mode="record",
        api_key="unused",
        max_api_calls=10,
        budget_usd=1,
        cache_path=tmp_path / "unused.json",
    )
    task = LLMTask(
        stage=LLMStage.PREPARE,
        input_text="bounded request",
        state_hash="campaign-state",
        prompt_version="campaign-v1",
        fallback_text='{"ok":false}',
        max_output_tokens=100,
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )
    router = LLMRouter(
        settings,
        provider=AccountedProvider(
            provider,
            local_b,
            settings,
            "prepare",
            "main",
            campaign_ledger=task_campaign_ledger(store_b),
        ),
    )
    assert router.complete(task) == '{"ok":false}'
    assert provider.calls == 0
    assert local_b.summary()["totals"]["calls"] == 0

    reloaded = task_campaign_ledger(ProjectStore(tmp_path / "other-project-root"))
    assert reloaded.summary()["totals"]["cost_nusd"] == 49_999_000_000
    with pytest.raises(StorageError, match="usage_limit_exceeded"):
        reloaded.reserve(
            request_key="would-overrun",
            operation="qa",
            branch_id=None,
            stage="review",
            model_id="fake",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.002,
        )


def test_local_preflight_failure_releases_only_its_campaign_reservation(
    project_store_at,
    counting_provider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "global-state"))
    store = project_store_at(tmp_path / "projects")
    campaign = task_campaign_ledger(store)
    blocked = UsageLedger(store, "project", "blocked")
    blocked.create(
        UsageLimits(max_calls=1, max_input_tokens=20000, max_output_tokens=1000, budget_usd=0)
    )
    existing = blocked.reserve(
        request_key="existing",
        operation="qa",
        branch_id=None,
        stage="prepare",
        model_id="fake",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0,
    )
    blocked.settle(existing, input_tokens=1, output_tokens=1, cost_usd=0, outcome="settled")
    settings = LLMSettings(
        mode="record",
        api_key="unused",
        max_api_calls=10,
        budget_usd=1,
        cache_path=tmp_path / "unused.json",
    )
    provider = counting_provider
    task = LLMTask(
        stage=LLMStage.PREPARE,
        input_text="request",
        state_hash="state",
        prompt_version="cancel-v1",
        fallback_text='{"ok":false}',
        max_output_tokens=50,
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )
    router = LLMRouter(
        settings,
        provider=AccountedProvider(
            provider, blocked, settings, "prepare", "main", campaign_ledger=campaign
        ),
    )
    assert router.complete(task) == '{"ok":false}'
    assert provider.calls == 0
    assert campaign.summary()["totals"]["calls"] == 0


def test_campaign_retains_uncertain_provider_charge_across_store_roots(
    project_store_at,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sheltersim.llm.accounting import runtime_usage_summary

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "global-state"))
    project_store = project_store_at(tmp_path / "project-root")
    local = UsageLedger(project_store, "project", "uncertain")
    # Output limits leave room for the router's thinking headroom on non-runtime stages.
    local.create(
        UsageLimits(max_calls=2, max_input_tokens=2_000_000, max_output_tokens=10_000, budget_usd=1)
    )
    campaign = task_campaign_ledger(project_store)

    class TimeoutProvider:
        def complete(self, **_kwargs):
            raise TimeoutError("the request may have reached the provider")

    settings = LLMSettings(
        mode="record",
        api_key="unused",
        max_api_calls=2,
        budget_usd=1,
        cache_path=tmp_path / "unused.json",
    )
    task = LLMTask(
        stage=LLMStage.REVIEW,
        input_text="short request",
        state_hash="state",
        prompt_version="uncertain-v1",
        fallback_text='{"ok":false}',
        max_output_tokens=50,
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )
    router = LLMRouter(
        settings,
        provider=AccountedProvider(
            TimeoutProvider(), local, settings, "review", "main", campaign_ledger=campaign
        ),
    )
    assert router.complete(task) == '{"ok":false}'
    assert local.summary()["uncertain_calls"] == 1
    assert campaign.summary()["uncertain_calls"] == 1

    summary = runtime_usage_summary(ProjectStore(tmp_path / "another-project-root"))
    assert summary["campaign"]["calls"] == 1
    assert summary["campaign"]["uncertain_calls"] == 1
    assert summary["campaign"]["remaining_usd"] < 50


def test_non_text_provider_response_settles_actual_usage_and_keeps_safe_error_category(
    project_store_at,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "global-state"))
    store = project_store_at(tmp_path / "projects")
    local = UsageLedger(store, "project", "empty-text-response")
    settings = LLMSettings(
        mode="record",
        api_key="unused",
        max_api_calls=2,
        max_input_tokens=4096,
        max_output_tokens=128,
        budget_usd=1,
        cache_path=tmp_path / "unused.json",
    )
    local.create(
        UsageLimits(max_calls=2, max_input_tokens=2_000_000, max_output_tokens=1000, budget_usd=1)
    )
    campaign = task_campaign_ledger(store)
    response_metadata = ProviderResponse(
        text="",
        model_id=settings.haiku_model,
        input_tokens=12,
        output_tokens=5,
        elapsed_ms=2.0,
    )

    class EmptyTextProvider:
        def complete(self, **_kwargs: object) -> ProviderResponse:
            raise LLMProviderError(
                "Anthropic API returned no text content",
                failure_kind="provider_refusal",
                response_metadata=response_metadata,
            )

    task = LLMTask(
        stage=LLMStage.RUNTIME,
        input_text="bounded test",
        state_hash="state",
        prompt_version="empty-text-response-v1",
        fallback_text='{"ok":false}',
        max_output_tokens=32,
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )
    router = LLMRouter(
        settings,
        provider=AccountedProvider(
            EmptyTextProvider(), local, settings, "runtime", "main", campaign_ledger=campaign
        ),
    )

    assert router.complete(task) == '{"ok":false}'
    assert router.records[0].outcome == "invalid_output"
    assert router.records[0].error_kind == "provider_refusal"
    assert router.records[0].input_tokens == 12
    assert router.records[0].output_tokens == 5
    assert local.summary()["uncertain_calls"] == 0
    assert campaign.summary()["uncertain_calls"] == 0
    assert local.summary()["by_error_code"] == {"provider_refusal": 1}
    assert campaign.summary()["by_error_code"] == {"provider_refusal": 1}


def test_campaign_settlement_counts_cache_and_hosted_search_fees(
    project_store_at,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "global-state"))
    store = project_store_at(tmp_path / "projects")
    local = UsageLedger(store, "project", "search-costs")
    settings = LLMSettings(
        mode="record",
        api_key="unused",
        max_api_calls=2,
        max_input_tokens=4096,
        max_output_tokens=128,
        budget_usd=10,
        cache_path=tmp_path / "unused.json",
    )
    local.create(
        UsageLimits(
            max_calls=2, max_input_tokens=2_000_000, max_output_tokens=10_000, budget_usd=10
        )
    )
    campaign = task_campaign_ledger(store)

    class SearchProvider:
        def complete(self, **_kwargs: object) -> ProviderResponse:
            return ProviderResponse(
                text='{"ok":true}',
                model_id="fake",
                input_tokens=100,
                output_tokens=20,
                elapsed_ms=1,
                cache_creation_5m_input_tokens=10,
                cache_creation_1h_input_tokens=20,
                cache_read_input_tokens=5,
                web_search_requests=1,
            )

    task = LLMTask(
        stage=LLMStage.PREPARE,
        input_text="short official search",
        state_hash="search-state",
        prompt_version="search-accounting-v1",
        fallback_text='{"ok":false}',
        max_output_tokens=64,
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        web_search_allowed_domains=("go.jp",),
        max_web_searches=1,
    )
    router = LLMRouter(
        settings,
        provider=AccountedProvider(
            SearchProvider(), local, settings, "region_search", "main", campaign_ledger=campaign
        ),
    )
    assert router.complete(task) == '{"ok":true}'
    assert campaign.summary()["totals"]["cost_nusd"] == 11_011_000
    receipt, _ = campaign.receipt_records({"operation": "region_search"})
    assert receipt[0]["input_tokens"] == 135
    assert receipt[0]["output_tokens"] == 20


def test_search_output_over_one_pass_does_not_freeze_ledgers(
    project_store_at,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "global-state"))
    store = project_store_at(tmp_path / "projects")
    settings = LLMSettings(
        mode="record",
        api_key="unused",
        max_api_calls=2,
        max_input_tokens=4096,
        max_output_tokens=256,
        budget_usd=10,
        cache_path=tmp_path / "unused.json",
    )
    from sheltersim.llm.accounting import usage_limits

    local = UsageLedger(store, "project", "search-passes")
    local.create(usage_limits(settings))
    campaign = task_campaign_ledger(store)

    class TwoPassSearchProvider:
        def complete(self, *, max_output_tokens: int, **_kwargs: object) -> ProviderResponse:
            # Server tools sample before and after the search; usage sums both passes.
            return ProviderResponse(
                text='{"ok":true}',
                model_id="fake",
                input_tokens=100,
                output_tokens=max_output_tokens + 253,
                elapsed_ms=1,
                web_search_requests=1,
            )

    task = LLMTask(
        stage=LLMStage.PREPARE,
        input_text="official search",
        state_hash="search-state",
        prompt_version="search-passes-v1",
        fallback_text='{"ok":false}',
        max_output_tokens=256,
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        web_search_allowed_domains=("go.jp",),
        max_web_searches=1,
    )
    router = LLMRouter(
        settings,
        provider=AccountedProvider(
            TwoPassSearchProvider(),
            local,
            settings,
            "region_search",
            "main",
            campaign_ledger=campaign,
        ),
    )

    assert router.complete(task) == '{"ok":true}'
    assert local.summary()["frozen"] is False
    assert campaign.summary()["frozen"] is False


def test_stored_tape_reloads_and_never_writes_sensitive_text(tmp_path: Path) -> None:
    from sheltersim.llm.accounting import StoredResponseTape

    store = ProjectStore(tmp_path)
    store.create_project("project")
    path = store._path("project", "usage-tapes", "draft.json")
    reply = ProviderResponse(
        text='{"ok":true}', model_id="m", input_tokens=1, output_tokens=1, elapsed_ms=0.0
    )

    StoredResponseTape(store, path).put("key", reply)
    assert StoredResponseTape(store, path).get("key") == reply

    risky = StoredResponseTape(store, store._path("project", "usage-tapes", "risky.json"))
    risky.put(
        "key",
        ProviderResponse(
            text="call 03-1234-5678", model_id="m", input_tokens=1, output_tokens=1, elapsed_ms=0.0
        ),
    )
    assert risky.get("key") is not None  # still usable within this request
    assert not store._path("project", "usage-tapes", "risky.json").exists()


class _AckProvider:
    """Answer like the real models do for the live model check's acknowledgement."""

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        del task, max_output_tokens, timeout_seconds
        return ProviderResponse(
            text='{"ok":true}', model_id=model_id, input_tokens=145, output_tokens=9, elapsed_ms=0
        )


def test_the_live_model_check_fits_its_call_and_budget_caps(live_model_check, tmp_path: Path):
    # tests/live/test_live_api.py runs this case against the real API only when asked, so
    # a ledger rule that denies one of its calls went unnoticed for days (#148).
    settings, router, tasks, _ = live_model_check(
        LLMSettings(api_key="mock-key"), tmp_path / "tape.json", provider=_AckProvider()
    )

    outputs = [router.complete(task) for task in tasks]

    assert outputs == ['{"ok":true}'] * 3
    assert [(record.stage, record.model_id, record.outcome) for record in router.records] == [
        ("runtime", settings.haiku_model, "recorded"),
        ("prepare", settings.opus_model, "recorded"),
        ("intervention", settings.sonnet_model, "recorded"),
    ]
    assert router.spent_usd <= settings.budget_usd
