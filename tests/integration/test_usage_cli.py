"""The usage CLI changes the task campaign ledger only with a matching confirmation."""

from __future__ import annotations

from pathlib import Path

import pytest

from sheltersim.contracts.workspace import UsageLimits
from sheltersim.storage.projects import ProjectStore
from sheltersim.storage.usage import task_campaign_ledger


def test_cli_unfreezes_the_campaign_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from sheltersim import cli
    from sheltersim.storage.usage import TASK_CAMPAIGN_ACCOUNT_ID

    monkeypatch.setattr(
        "sheltersim.storage.usage.task_campaign_store",
        lambda: ProjectStore(tmp_path / "state"),
    )
    campaign = task_campaign_ledger()
    receipt = campaign.reserve(
        request_key="k",
        operation="review",
        branch_id=None,
        stage="review",
        model_id="m",
        input_tokens=10,
        output_tokens=10,
        cost_usd=0.001,
    )
    campaign.settle(receipt, input_tokens=10, output_tokens=20, cost_usd=0.001, outcome="settled")
    assert campaign.summary()["frozen"] is True

    with pytest.raises(SystemExit):
        cli.main(
            [
                "usage",
                "--unfreeze",
                TASK_CAMPAIGN_ACCOUNT_ID,
                "--confirm",
                "wrong",
                "--reason",
                "reviewed",
            ]
        )
    assert campaign.summary()["frozen"] is True

    assert (
        cli.main(
            [
                "usage",
                "--unfreeze",
                TASK_CAMPAIGN_ACCOUNT_ID,
                "--confirm",
                TASK_CAMPAIGN_ACCOUNT_ID,
                "--reason",
                "server-tool passes reviewed",
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["unfreeze_event"]["reason"] == "server-tool passes reviewed"
    assert campaign.summary()["frozen"] is False


def test_cli_increases_campaign_budget_only_with_confirmation_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from sheltersim import cli
    from sheltersim.storage import usage
    from sheltersim.storage.usage import (
        TASK_CAMPAIGN_ACCOUNT_ID,
        UsageLedger,
        task_campaign_ledger,
    )

    monkeypatch.setattr(
        usage,
        "task_campaign_store",
        lambda: ProjectStore(tmp_path / "state"),
    )
    store = usage.task_campaign_store()
    store.create_project("runtime-accounts", _internal=True)
    campaign = UsageLedger(store, "runtime-accounts", TASK_CAMPAIGN_ACCOUNT_ID)
    campaign.create(
        UsageLimits(
            max_calls=100_000,
            max_input_tokens=10_000_000_000,
            max_output_tokens=10_000_000_000,
            budget_usd=50.0,
            max_concurrency=16,
        )
    )
    receipt = campaign.reserve(
        request_key="before-increase",
        operation="test",
        branch_id=None,
        stage="runtime",
        model_id="test-model",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
    )
    campaign.settle(receipt, input_tokens=10, output_tokens=5, cost_usd=0.001, outcome="settled")

    with pytest.raises(SystemExit):
        cli.main(
            [
                "usage",
                "--increase-budget",
                TASK_CAMPAIGN_ACCOUNT_ID,
                "--new-budget-usd",
                "90",
                "--confirm",
                "wrong",
                "--reason",
                "owner approved additional allowance",
            ]
        )
    assert campaign.summary()["limits"]["budget_usd"] == 50.0

    assert (
        cli.main(
            [
                "usage",
                "--increase-budget",
                TASK_CAMPAIGN_ACCOUNT_ID,
                "--new-budget-usd",
                "90",
                "--confirm",
                TASK_CAMPAIGN_ACCOUNT_ID,
                "--reason",
                "owner approved additional allowance",
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["budget_increase_event"]["previous_budget_usd"] == 50.0
    summary = campaign.summary()
    assert summary["limits"]["budget_usd"] == 90.0
    assert summary["totals"]["calls"] == 1
    assert summary["budget_increase_events"][-1]["new_budget_usd"] == 90.0
    assert task_campaign_ledger().summary()["limits"]["budget_usd"] == 90.0
