"""Running a frozen Scenario from the CLI: the LLM status is printed, a rule fallback is
warned about, an offline run is not, and each run opens its own usage account.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim import cli
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import LLMAllowanceError
from sheltersim.llm.provider import LLMRouter


class DeniedAllowanceProvider:
    def complete(self, **_kwargs):
        raise LLMAllowanceError("usage_allowance_denied:usage_limit_exceeded")


def _router(provider, *, max_api_calls: int = 1_000) -> LLMRouter:
    settings = LLMSettings(
        mode=LLMMode.RECORD, api_key="mock-key", max_api_calls=max_api_calls, budget_usd=1.0
    )
    return LLMRouter(settings, provider=provider, tape=ResponseTape())


def _frozen_scenario(tmp_path: Path, scenario) -> Path:
    source = tmp_path / "scenario.json"
    source.write_text(scenario.canonical_json(), encoding="utf-8")
    frozen = tmp_path / "approved.json"
    arguments = ["prepare", "--scenario", str(source), "--freeze", "--output", str(frozen)]
    assert cli.main(arguments) == 0
    return frozen


def test_cli_run_prints_status_and_warns_on_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    small_benchmark_scenario,
) -> None:
    monkeypatch.setenv("SHELTERSIM_PROJECTS_DIR", str(tmp_path / "projects"))
    frozen = _frozen_scenario(tmp_path, small_benchmark_scenario())
    capsys.readouterr()
    monkeypatch.setattr(
        cli, "_cli_accounted_router", lambda *_args, **_kwargs: _router(DeniedAllowanceProvider())
    )

    assert cli.main(["run", "--scenario", str(frozen), "--output", str(tmp_path / "run")]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["llm_status"]["status"] == "all_fallback"
    assert "warning: LLM fallback:" in captured.err
    assert "shared_allowance_unavailable" in captured.err


def test_cli_offline_run_does_not_warn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    small_benchmark_scenario,
) -> None:
    monkeypatch.setenv("SHELTERSIM_PROJECTS_DIR", str(tmp_path / "projects"))
    frozen = _frozen_scenario(tmp_path, small_benchmark_scenario())
    capsys.readouterr()

    assert (
        cli.main(["run", "--offline", "--scenario", str(frozen), "--output", str(tmp_path / "run")])
        == 0
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out)["llm_status"]["status"] == "not_requested"
    # The fixture selects no psychology model, so only the legacy-model notice may appear.
    assert [line for line in captured.err.splitlines() if "legacy model" not in line] == []


def test_each_cli_run_opens_its_own_usage_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    small_benchmark_scenario,
) -> None:
    monkeypatch.setenv("SHELTERSIM_PROJECTS_DIR", str(tmp_path / "projects"))
    frozen = _frozen_scenario(tmp_path, small_benchmark_scenario(30))
    accounts = []
    for name in ("first", "second"):
        output = tmp_path / name
        assert (
            cli.main(["run", "--offline", "--scenario", str(frozen), "--output", str(output)]) == 0
        )
        document = json.loads(cli._cli_usage_account_path(output).read_text(encoding="utf-8"))
        accounts.append(document["account_id"])
    capsys.readouterr()

    assert all(account.startswith("cli-run-") for account in accounts)
    assert accounts[0] != accounts[1]
