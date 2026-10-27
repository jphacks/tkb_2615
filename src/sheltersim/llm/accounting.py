"""Provider-boundary accounting; deterministic historical replay never uses this adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sheltersim.contracts.workspace import UsageLimits
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMSettings
from sheltersim.llm.contracts import LLMAllowanceError, LLMStage, LLMTask, ProviderResponse
from sheltersim.llm.provider import (
    THINKING_HEADROOM_TOKENS,
    LLMRouter,
    Provider,
    output_reservation_tokens,
)
from sheltersim.storage.projects import ProjectStore, StorageError, digest, safe_document
from sheltersim.storage.usage import UsageLedger, task_campaign_ledger, task_campaign_summary

RUNTIME_ACCOUNTS_PROJECT_ID = "runtime-accounts"
# Calls a Run's runtime decisions must leave on the shared account: the post-run report
# plus two results questions. Without this, a long Run used every call before its report.
ANALYSIS_RESERVED_CALLS = 3


def usage_limits(settings: LLMSettings) -> UsageLimits:
    return UsageLimits(
        max_calls=settings.max_api_calls,
        max_input_tokens=settings.max_api_calls * max(1_000_000, settings.max_input_tokens + 2048),
        # Reservations include the router's thinking headroom on top of each answer cap.
        max_output_tokens=settings.max_api_calls
        * (settings.max_output_tokens + THINKING_HEADROOM_TOKENS),
        budget_usd=settings.budget_usd,
        max_concurrency=settings.max_concurrency,
    )


class AccountedProvider:
    def __init__(
        self,
        provider: Provider,
        ledger: UsageLedger,
        settings: LLMSettings,
        operation: str,
        branch_id: str | None,
        campaign_ledger: UsageLedger | None = None,
    ) -> None:
        self.provider, self.ledger, self.settings = provider, ledger, settings
        self.operation, self.branch_id = operation, branch_id
        self.campaign_ledger = campaign_ledger

    def complete(
        self, *, model_id: str, task: LLMTask, max_output_tokens: int, timeout_seconds: float
    ) -> ProviderResponse:
        # UTF-8 byte counts deliberately overestimate token use, including system/schema.
        bound = len(task.input_text.encode()) + len(json.dumps(task.output_schema).encode()) + 1024
        price = self.settings.price_for(model_id)
        input_reservation = max(bound, 1_000_000 if task.max_web_searches else 0)
        output_reservation = output_reservation_tokens(task, max_output_tokens)
        reserve = (
            input_reservation * price.cache_creation_1h_usd_per_million
            + output_reservation * price.output_usd_per_million
        ) / 1_000_000 + task.max_web_searches * 0.01
        key = digest(
            {
                "task": task.cache_key(model_id),
                "operation": self.operation,
                "branch_id": self.branch_id,
            }
        )
        campaign_receipt = None
        if self.campaign_ledger is not None:
            campaign_key = digest(
                {
                    "local_account": self.ledger.account_id,
                    "local_project": self.ledger.project_id,
                    "request_key": key,
                }
            )
            try:
                campaign_receipt = self.campaign_ledger.reserve(
                    request_key=campaign_key,
                    operation=self.operation,
                    branch_id=self.branch_id,
                    stage=task.stage.value,
                    model_id=model_id,
                    input_tokens=input_reservation,
                    output_tokens=output_reservation,
                    cost_usd=reserve,
                    identity={
                        "scope": "task_campaign",
                        "input_hash": task.input_hash,
                        "state_hash": task.state_hash,
                        "input_usd_per_million": price.input_usd_per_million,
                        "output_usd_per_million": price.output_usd_per_million,
                    },
                )
            except (StorageError, OSError) as exc:
                code = exc.code if isinstance(exc, StorageError) else "usage_storage_unavailable"
                raise LLMAllowanceError(
                    f"task_campaign_allowance_denied:{code}; no provider request was made"
                ) from exc
        try:
            receipt = self.ledger.reserve(
                request_key=key,
                operation=self.operation,
                branch_id=self.branch_id,
                stage=task.stage.value,
                model_id=model_id,
                input_tokens=input_reservation,
                output_tokens=output_reservation,
                cost_usd=reserve,
                keep_calls_free=(ANALYSIS_RESERVED_CALLS if task.stage is LLMStage.RUNTIME else 0),
                identity={
                    "input_hash": task.input_hash,
                    "state_hash": task.state_hash,
                    "prompt_version": task.prompt_version,
                    "schema_version": task.schema_version,
                    "input_usd_per_million": price.input_usd_per_million,
                    "output_usd_per_million": price.output_usd_per_million,
                },
            )
        except (StorageError, OSError) as exc:
            if campaign_receipt is not None:
                try:
                    self.campaign_ledger.cancel_before_provider(campaign_receipt)
                except (StorageError, OSError):
                    # A conservative pending reservation is safer than risking a refund
                    # if the local store's state is no longer known.
                    pass
            code = exc.code if isinstance(exc, StorageError) else "usage_storage_unavailable"
            raise LLMAllowanceError(
                f"usage_allowance_denied:{code}; no provider request was made"
            ) from exc
        try:
            response = self.provider.complete(
                model_id=model_id,
                task=task,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
            )
        except BaseException as exc:
            status = getattr(exc, "status_code", None)
            code = _usage_error_code(exc, status)
            response_metadata = getattr(exc, "response_metadata", None)
            if isinstance(response_metadata, ProviderResponse):
                cache_creation = (
                    response_metadata.cache_creation_5m_input_tokens
                    + response_metadata.cache_creation_1h_input_tokens
                )
                total_input = (
                    response_metadata.input_tokens
                    + cache_creation
                    + response_metadata.cache_read_input_tokens
                )
                price = self.settings.price_for(model_id)
                cost = _provider_response_cost(response_metadata, price)
                self.ledger.settle(
                    receipt,
                    input_tokens=total_input,
                    output_tokens=response_metadata.output_tokens,
                    cost_usd=cost,
                    outcome="settled",
                    error_code=code,
                )
                if campaign_receipt is not None:
                    self.campaign_ledger.settle(
                        campaign_receipt,
                        input_tokens=total_input,
                        output_tokens=response_metadata.output_tokens,
                        cost_usd=cost,
                        outcome="settled",
                        error_code=code,
                    )
            else:
                self.ledger.settle(
                    receipt,
                    input_tokens=input_reservation,
                    output_tokens=output_reservation,
                    cost_usd=reserve,
                    outcome="uncertain",
                    error_code=code,
                )
                if campaign_receipt is not None:
                    self.campaign_ledger.settle(
                        campaign_receipt,
                        input_tokens=input_reservation,
                        output_tokens=output_reservation,
                        cost_usd=reserve,
                        outcome="uncertain",
                        error_code=code,
                    )
            raise
        cache_creation = (
            response.cache_creation_5m_input_tokens + response.cache_creation_1h_input_tokens
        )
        total_input = response.input_tokens + cache_creation + response.cache_read_input_tokens
        token_cost = (
            response.input_tokens * price.input_usd_per_million
            + response.cache_creation_5m_input_tokens * price.cache_creation_5m_usd_per_million
            + response.cache_creation_1h_input_tokens * price.cache_creation_1h_usd_per_million
            + response.cache_read_input_tokens * price.cache_read_usd_per_million
            + response.output_tokens * price.output_usd_per_million
        ) / 1_000_000
        cost = token_cost + response.web_search_requests * 0.01
        self.ledger.settle(
            receipt,
            input_tokens=total_input,
            output_tokens=response.output_tokens,
            cost_usd=cost,
            outcome="settled",
        )
        if campaign_receipt is not None:
            self.campaign_ledger.settle(
                campaign_receipt,
                input_tokens=total_input,
                output_tokens=response.output_tokens,
                cost_usd=cost,
                outcome="settled",
            )
        return response


class StoredResponseTape(ResponseTape):
    """Accepted replies kept in the Project store for one draft or Run analysis.

    The ledger never pays twice for an identical request, so without the earlier reply a
    repeated preview or question could only fall back. A tape whose sanitized copy differs
    (contact-like or secret-like text) stays in memory and is not written.
    """

    def __init__(self, store: ProjectStore, path: Path) -> None:
        self._store, self._file = store, path
        entries = ResponseTape.from_dict(store._read(path)).entries if path.exists() else {}
        super().__init__(entries=entries)

    def save(self) -> None:
        document = self.to_dict()
        if safe_document(document) != document:
            return
        with self._store.transaction():
            self._store._write(self._file, document)


def accounted_router(
    store: ProjectStore,
    project_id: str,
    *,
    run_id: str | None = None,
    account_id: str | None = None,
    operation: str,
    branch_id: str | None = None,
    settings: LLMSettings | None = None,
    tape: ResponseTape | None = None,
) -> LLMRouter:
    settings = settings or LLMSettings.from_env()
    if run_id is not None:
        ledger = UsageLedger.for_run(store, project_id, run_id, usage_limits(settings), account_id)
    elif account_id is not None:
        ledger = UsageLedger(store, project_id, account_id)
        ledger.create(usage_limits(settings))
    else:
        raise StorageError("usage account or Run is required")
    router = LLMRouter(settings, tape=tape)
    # Offline and tape/cache paths do not call the live provider and incur no reservations.
    if router.mode.value == "record":
        router.provider = AccountedProvider(
            router.provider,
            ledger,
            settings,
            operation,
            branch_id,
            campaign_ledger=task_campaign_ledger(store),
        )
    return router


def runtime_accounted_router(
    store: ProjectStore,
    *,
    account_id: str,
    operation: str,
    settings: LLMSettings | None = None,
    branch_id: str | None = None,
) -> LLMRouter:
    """Use the shared durable ledger for non-Project CLI and legacy-Demo routes."""
    store.create_project(RUNTIME_ACCOUNTS_PROJECT_ID, _internal=True)
    return accounted_router(
        store,
        RUNTIME_ACCOUNTS_PROJECT_ID,
        account_id=account_id,
        operation=operation,
        branch_id=branch_id,
        settings=settings,
    )


def runtime_usage_summary(store: ProjectStore) -> dict[str, Any]:
    """Return bounded, sanitized summaries and recent receipts for CLI/Demo scopes."""
    campaign = task_campaign_summary()
    project_path = store._path(RUNTIME_ACCOUNTS_PROJECT_ID, "project.json")
    if not project_path.exists():
        return {
            "schema_version": 1,
            "coverage": "no_runtime_account_created",
            "accounts": [],
            "campaign": campaign,
            "unrecorded_history": "unknown",
        }
    usage_dir = store._path(RUNTIME_ACCOUNTS_PROJECT_ID, "usage")
    accounts = []
    for path in sorted(usage_dir.glob("*.json")) if usage_dir.exists() else ():
        document = store._read(path)
        account_id = document.get("account_id")
        scope = (
            "legacy_demo"
            if account_id == "legacy_demo"
            else "cli"
            if isinstance(account_id, str) and account_id.startswith("cli-")
            else None
        )
        if scope is None:
            continue
        ledger = UsageLedger(store, RUNTIME_ACCOUNTS_PROJECT_ID, account_id)
        receipts, anchor = ledger.receipt_records({})
        accounts.append(
            {
                "scope": scope,
                "account_id": account_id,
                "summary": ledger.summary(),
                "recent_receipts": receipts[-20:],
                "receipt_count": len(receipts),
                "receipts_truncated": len(receipts) > 20,
                "receipts_anchor": anchor,
            }
        )
    totals_by_scope: dict[str, dict[str, int]] = {}
    for account in accounts:
        totals = totals_by_scope.setdefault(
            account["scope"],
            {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_nusd": 0,
            },
        )
        for key, value in account["summary"]["totals"].items():
            totals[key] += value
    return {
        "schema_version": 1,
        "coverage": "reservations_and_settlements_since_account_creation",
        "accounts": accounts,
        "campaign": campaign,
        "totals_by_scope": totals_by_scope,
        "unrecorded_history": "unknown_not_zero",
        "storage_scope": "one task-wide campaign ledger shared by every Project, Run, CLI, Web worker and process using this configured ProjectStore root",
    }


def runtime_usage_store(root: str | Path | None = None) -> ProjectStore:
    """Resolve the shared local accounting store without persisting source paths."""
    import os

    return ProjectStore(root or os.environ.get("SHELTERSIM_PROJECTS_DIR", "runs/.projects"))


def _usage_error_code(exc: BaseException, status: object) -> str:
    if not isinstance(exc, Exception):
        return "interrupted"
    if status == 429:
        return "http_429"
    if type(status) is int and 400 <= status < 500:
        return "http_4xx"
    if type(status) is int and 500 <= status < 600:
        return "http_5xx"
    category = getattr(exc, "failure_kind", None)
    if category in {
        "provider_refusal",
        "thinking_without_text",
        "non_text_response",
        "empty_text_response",
    }:
        return category
    return "provider_timeout_or_transport"


def _provider_response_cost(response: ProviderResponse, price: object) -> float:
    return (
        response.input_tokens * price.input_usd_per_million
        + response.cache_creation_5m_input_tokens * price.cache_creation_5m_usd_per_million
        + response.cache_creation_1h_input_tokens * price.cache_creation_1h_usd_per_million
        + response.cache_read_input_tokens * price.cache_read_usd_per_million
        + response.output_tokens * price.output_usd_per_million
        + response.web_search_requests * 10_000.0
    ) / 1_000_000
