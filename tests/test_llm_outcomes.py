"""Offline checks that rule fallbacks are counted and surfaced, never reported as clean runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim import cli
from sheltersim.core.world import WorldState
from sheltersim.experiments.benchmark import build_benchmark_scenario
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import LLMAllowanceError, ProviderResponse
from sheltersim.llm.outcomes import fallback_warning, summarize_call_outcomes
from sheltersim.llm.provider import LLMRouter
from sheltersim.storage.projects import ProjectStore, digest
from sheltersim.web.projects import ProjectAPI

BASELINE = Path(__file__).parents[1] / "fixtures" / "scenarios" / "baseline_100_7d.json"


class AcceptingProvider:
    """Return a valid non-fallback answer for runtime and review tasks."""

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        del max_output_tokens, timeout_seconds
        properties = task.output_schema["properties"]
        if "priority" in properties:
            text = '{"priority":1,"rationale":"synthetic urgent"}'
        elif set(properties) == {"action"}:
            text = '{"action":"offer_reassurance"}'
        else:
            text = task.fallback_text
        return ProviderResponse(
            text=text, model_id=model_id, input_tokens=10, output_tokens=5, elapsed_ms=0.0
        )


class DeniedAllowanceProvider:
    def complete(self, **_kwargs):
        raise LLMAllowanceError("usage_allowance_denied:usage_limit_exceeded")


def _scenario(duration_minutes: int = 30) -> ScenarioSpec:
    # Thirty synthetic Agents raise several pending support requests at minute 0.
    return build_benchmark_scenario(
        ScenarioSpec.load(BASELINE), agent_count=30, seed=7, duration_minutes=duration_minutes
    )


def _router(provider, *, max_api_calls: int = 1_000) -> LLMRouter:
    settings = LLMSettings(
        mode=LLMMode.RECORD, api_key="mock-key", max_api_calls=max_api_calls, budget_usd=1.0
    )
    return LLMRouter(settings, provider=provider, tape=ResponseTape())


def _manifest(run_directory: Path) -> dict[str, object]:
    return json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))


def _record(
    index: int,
    outcome: str,
    *,
    fallback: bool,
    error_kind: str | None = None,
    prompt_version: str = "support-priority-v1",
) -> dict[str, object]:
    return {
        "call_index": index,
        "outcome": outcome,
        "fallback_used": fallback,
        "error_kind": error_kind,
        "prompt_version": prompt_version,
    }


def test_summary_classifies_every_status() -> None:
    assert summarize_call_outcomes([])["status"] == "not_requested"
    offline = [_record(0, "offline", fallback=True)]
    assert summarize_call_outcomes(offline)["status"] == "offline_rules"
    assert (
        summarize_call_outcomes(
            [_record(0, "recorded", fallback=False), _record(1, "cache_hit", fallback=False)]
        )["status"]
        == "all_accepted"
    )
    assert (
        summarize_call_outcomes([_record(0, "shared_allowance_unavailable", fallback=True)])[
            "status"
        ]
        == "all_fallback"
    )


def test_summary_counts_caps_and_validation_failures_as_fallbacks() -> None:
    summary = summarize_call_outcomes(
        [
            _record(0, "invalid_output", fallback=True, error_kind="schema_validation_failed"),
            _record(1, "recorded", fallback=False),
            _record(
                2,
                "max_api_calls",
                fallback=True,
                error_kind="max_api_calls",
                prompt_version="event-triggered-social-v2",
            ),
            _record(3, "max_api_calls", fallback=True, error_kind="max_api_calls"),
        ]
    )

    assert summary["status"] == "degraded"
    assert summary["accepted_count"] == 1
    assert summary["fallback_count"] == 3
    assert summary["fallback_by_reason"] == {"invalid_output": 1, "max_api_calls": 2}
    assert summary["fallback_by_error_kind"] == {
        "max_api_calls": 2,
        "schema_validation_failed": 1,
    }
    assert summary["fallback_by_prompt_version"] == {
        "event-triggered-social-v2": 1,
        "support-priority-v1": 2,
    }
    assert summary["first_fallback_call_index"] == 0
    warning = fallback_warning(summary)
    assert warning is not None and "3 of 4" in warning and "max_api_calls: 2" in warning
    clean = summarize_call_outcomes([_record(0, "recorded", fallback=False)])
    assert fallback_warning(clean) is None


def test_capped_run_manifest_reports_degraded_fallbacks(tmp_path: Path) -> None:
    router = _router(AcceptingProvider(), max_api_calls=3)
    run_scenario(_scenario(), tmp_path / "run", llm_router=router)

    usage = _manifest(tmp_path / "run")["llm_usage"]
    outcomes = usage["outcomes"]
    assert outcomes["status"] == "degraded"
    assert outcomes["accepted_count"] == 3
    assert outcomes["fallback_by_reason"]["max_api_calls"] == outcomes["fallback_count"] > 0
    # Call-cap stops were previously omitted from this count.
    assert usage["failures"] == outcomes["fallback_count"]


def test_denied_allowance_run_manifest_reports_all_fallback(tmp_path: Path) -> None:
    run_scenario(_scenario(), tmp_path / "run", llm_router=_router(DeniedAllowanceProvider()))

    usage = _manifest(tmp_path / "run")["llm_usage"]
    assert usage["api_request_count"] == 0
    assert usage["outcomes"]["status"] == "all_fallback"
    assert set(usage["outcomes"]["fallback_by_reason"]) == {"shared_allowance_unavailable"}
    assert usage["failures"] == usage["outcomes"]["call_count"] > 0


def test_offline_run_manifest_has_no_requested_tasks(tmp_path: Path) -> None:
    run_scenario(_scenario(), tmp_path / "run")

    usage = _manifest(tmp_path / "run")["llm_usage"]
    assert usage["outcomes"]["status"] == "not_requested"
    assert usage["failures"] == 0


def _frozen_scenario(tmp_path: Path) -> Path:
    source = tmp_path / "scenario.json"
    source.write_text(_scenario().canonical_json(), encoding="utf-8")
    frozen = tmp_path / "approved.json"
    arguments = ["prepare", "--scenario", str(source), "--freeze", "--output", str(frozen)]
    assert cli.main(arguments) == 0
    return frozen


def test_cli_run_prints_status_and_warns_on_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SHELTERSIM_PROJECTS_DIR", str(tmp_path / "projects"))
    frozen = _frozen_scenario(tmp_path)
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SHELTERSIM_PROJECTS_DIR", str(tmp_path / "projects"))
    frozen = _frozen_scenario(tmp_path)
    capsys.readouterr()

    assert (
        cli.main(["run", "--offline", "--scenario", str(frozen), "--output", str(tmp_path / "run")])
        == 0
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out)["llm_status"]["status"] == "not_requested"
    assert captured.err == ""


def test_project_branch_llm_status_reads_head_checkpoint_records(tmp_path: Path) -> None:
    scenario = _scenario()
    store = ProjectStore(tmp_path)
    store.create_project("project")
    saved = store.save_scenario(
        "project", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
    )
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    runtime = BranchRuntime.from_world(WorldState.from_scenario(scenario))
    router = _router(DeniedAllowanceProvider())
    timeline = TimelineStore(store, "project", "run")
    timeline.record(runtime, router=router)
    timeline.advance("main", 30, router=router)
    head_before = store.branch_reference("project", "run", "main").checkpoint_hash
    api = ProjectAPI(store, recover=False)
    try:
        status = api.get(["project", "runs", "run", "branches", "main", "llm-status"], {})
    finally:
        api.close()

    assert status["mode"] == "record"
    assert status["computed_until"] == 30
    assert status["outcomes"]["status"] == "all_fallback"
    assert status["outcomes"]["fallback_count"] == len(router.records) > 0
    # Reading the status is observational and leaves the branch head unchanged.
    assert store.branch_reference("project", "run", "main").checkpoint_hash == head_before
