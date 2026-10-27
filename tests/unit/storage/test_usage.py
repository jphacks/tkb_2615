"""A usage ledger keeps what was spent: reloading, unfreezing or holding back never refunds it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sheltersim.contracts.workspace import UsageLimits
from sheltersim.storage.projects import ProjectStore, StorageError
from sheltersim.storage.usage import UsageLedger


def test_uncertain_receipt_survives_reload_without_refund(project_store_at, tmp_path: Path) -> None:
    store = project_store_at(tmp_path)
    ledger = UsageLedger(store, "project", "account")
    ledger.create(
        UsageLimits(max_calls=1, max_input_tokens=20000, max_output_tokens=1000, budget_usd=1)
    )
    receipt = ledger.reserve(
        request_key="input",
        operation="compiler",
        branch_id="main",
        stage="prepare",
        model_id="fake",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.1,
    )
    ledger.settle(receipt, input_tokens=0, output_tokens=0, cost_usd=0, outcome="uncertain")
    reloaded = UsageLedger(store, "project", "account")
    reloaded.create(
        UsageLimits(
            max_calls=100, max_input_tokens=100000, max_output_tokens=100000, budget_usd=100
        )
    )
    assert reloaded.summary()["limits"]["max_calls"] == 1
    assert reloaded.summary()["totals"]["cost_nusd"] == 100000000
    with pytest.raises(StorageError, match="usage_limit_exceeded"):
        reloaded.reserve(
            request_key="another",
            operation="questions",
            branch_id="child",
            stage="review",
            model_id="fake",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.001,
        )


def _frozen_ledger(store: ProjectStore, account_id: str) -> UsageLedger:
    ledger = UsageLedger(store, "project", account_id)
    ledger.create(
        UsageLimits(max_calls=2, max_input_tokens=1000, max_output_tokens=1000, budget_usd=1)
    )
    receipt = ledger.reserve(
        request_key="k",
        operation="review",
        branch_id=None,
        stage="review",
        model_id="m",
        input_tokens=10,
        output_tokens=10,
        cost_usd=0.001,
    )
    # Actual output above the reservation freezes the account, as a server-tool call did.
    ledger.settle(receipt, input_tokens=10, output_tokens=20, cost_usd=0.001, outcome="settled")
    return ledger


def test_unfreeze_requires_confirmation_and_keeps_recorded_spend(
    project_store_at, tmp_path: Path
) -> None:
    store = project_store_at(tmp_path)
    ledger = _frozen_ledger(store, "frozen")
    assert ledger.summary()["frozen"] is True
    with pytest.raises(StorageError, match="confirmation_mismatch"):
        ledger.unfreeze(confirm_account_id="other", reason="reviewed")
    with pytest.raises(StorageError, match="reason_required"):
        ledger.unfreeze(confirm_account_id="frozen", reason=" ")

    event = ledger.unfreeze(confirm_account_id="frozen", reason="server-tool passes reviewed")

    summary = ledger.summary()
    assert summary["frozen"] is False
    assert summary["totals"]["output_tokens"] == 20  # spend history is untouched
    assert summary["unfreeze_events"] == [event]
    assert event["receipt_count"] == 1
    with pytest.raises(StorageError, match="not_frozen"):
        ledger.unfreeze(confirm_account_id="frozen", reason="again")


def test_unfreeze_refuses_pending_reservations(project_store_at, tmp_path: Path) -> None:
    store = project_store_at(tmp_path)
    ledger = _frozen_ledger(store, "busy")
    document = store._read(ledger.path)
    document["receipts"]["open"] = {
        **next(iter(document["receipts"].values())),
        "status": "pending",
        "pid": os.getpid(),
    }
    store._write(ledger.path, document)

    with pytest.raises(StorageError, match="pending_reservations"):
        ledger.unfreeze(confirm_account_id="busy", reason="reviewed")


def test_unfreeze_keeps_the_ledger_v1_and_audits_beside_it(
    project_store_at, tmp_path: Path
) -> None:
    store = project_store_at(tmp_path)
    ledger = _frozen_ledger(store, "audited")

    ledger.unfreeze(confirm_account_id="audited", reason="reviewed")

    # Older readers of a machine-wide ledger only accept schema_version 1.
    assert store._read(ledger.path)["schema_version"] == 1
    assert "unfreeze_events" not in store._read(ledger.path)
    assert len(store._read(ledger.audit_path)["events"]) == 1
    assert [path.name for path in ledger.path.parent.iterdir()] == ["audited.json"]


def test_ledger_keeps_requested_calls_free_for_later_work(project_store_at, tmp_path: Path) -> None:
    store = project_store_at(tmp_path)
    ledger = UsageLedger(store, "project", "shared-run")
    ledger.create(
        UsageLimits(max_calls=5, max_input_tokens=10_000, max_output_tokens=10_000, budget_usd=1)
    )

    def reserve(index: int, keep: int) -> str:
        receipt = ledger.reserve(
            request_key=f"k{index}",
            operation="runtime",
            branch_id=None,
            stage="runtime",
            model_id="m",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0001,
            keep_calls_free=keep,
        )
        ledger.settle(receipt, input_tokens=1, output_tokens=1, cost_usd=0.0001, outcome="settled")
        return receipt

    reserve(0, keep=3)
    reserve(1, keep=3)
    with pytest.raises(StorageError, match="usage_runtime_share_exhausted"):
        reserve(2, keep=3)
    for index in (3, 4, 5):  # the report and questions may use the held-back calls
        reserve(index, keep=0)
    with pytest.raises(StorageError, match="usage_limit_exceeded"):
        reserve(6, keep=0)

    single = UsageLedger(store, "project", "single-call")
    single.create(UsageLimits(max_calls=1, max_input_tokens=10, max_output_tokens=10, budget_usd=1))
    single.reserve(
        request_key="only",
        operation="runtime",
        branch_id=None,
        stage="runtime",
        model_id="m",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0001,
        keep_calls_free=3,
    )
