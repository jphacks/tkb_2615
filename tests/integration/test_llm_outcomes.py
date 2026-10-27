"""Offline checks that rule fallbacks are counted and surfaced in Run manifests, Project
status and reports, never reported as clean runs.

The outcome summary itself is checked in tests/unit/llm/test_outcomes.py, and the CLI
warnings in tests/e2e/test_cli_run.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim import cli
from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import LLMAllowanceError, ProviderResponse
from sheltersim.llm.provider import LLMRouter
from sheltersim.storage.projects import ProjectStore, digest
from sheltersim.web.projects import ProjectAPI


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


def _manifest(run_directory: Path) -> dict[str, object]:
    return json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))


class TruncatedProvider:
    """Reply with cut-off JSON, as a response stopped at max_tokens would."""

    def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
        del max_output_tokens, timeout_seconds
        return ProviderResponse(
            text='{"profiles":[{"profile_index":0,',
            model_id=model_id,
            input_tokens=10,
            output_tokens=5,
            elapsed_ms=0.0,
        )


def test_capped_run_manifest_reports_degraded_fallbacks(
    tmp_path: Path, small_benchmark_scenario, recording_router
) -> None:
    router = recording_router(AcceptingProvider(), max_api_calls=3)
    run_scenario(small_benchmark_scenario(), tmp_path / "run", llm_router=router)

    usage = _manifest(tmp_path / "run")["llm_usage"]
    outcomes = usage["outcomes"]
    assert outcomes["status"] == "degraded"
    assert outcomes["accepted_count"] == 3
    assert outcomes["fallback_by_reason"]["max_api_calls"] == outcomes["fallback_count"] > 0
    # Call-cap stops were previously omitted from this count.
    assert usage["failures"] == outcomes["fallback_count"]


def test_denied_allowance_run_manifest_reports_all_fallback(
    tmp_path: Path, small_benchmark_scenario, recording_router
) -> None:
    run_scenario(
        small_benchmark_scenario(),
        tmp_path / "run",
        llm_router=recording_router(DeniedAllowanceProvider()),
    )

    usage = _manifest(tmp_path / "run")["llm_usage"]
    assert usage["api_request_count"] == 0
    assert usage["outcomes"]["status"] == "all_fallback"
    assert set(usage["outcomes"]["fallback_by_reason"]) == {"shared_allowance_unavailable"}
    assert usage["failures"] == usage["outcomes"]["call_count"] > 0


def test_offline_run_manifest_has_no_requested_tasks(
    tmp_path: Path, small_benchmark_scenario
) -> None:
    run_scenario(small_benchmark_scenario(), tmp_path / "run")

    usage = _manifest(tmp_path / "run")["llm_usage"]
    assert usage["outcomes"]["status"] == "not_requested"
    assert usage["failures"] == 0


def test_project_branch_llm_status_reads_head_checkpoint_records(
    tmp_path: Path, small_benchmark_scenario, recording_router
) -> None:
    scenario = small_benchmark_scenario()
    store = ProjectStore(tmp_path)
    store.create_project("project")
    saved = store.save_scenario(
        "project", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
    )
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    runtime = BranchRuntime.from_world(WorldState.from_scenario(scenario))
    router = recording_router(DeniedAllowanceProvider())
    timeline = TimelineStore(store, "project", "run")
    timeline.record(runtime, router=router)
    timeline.advance("main", 200, router=router)
    head_before = store.branch_reference("project", "run", "main").checkpoint_hash
    api = ProjectAPI(store, recover=False)
    try:
        status = api.get(["project", "runs", "run", "branches", "main", "llm-status"], {})
    finally:
        api.close()

    assert status["mode"] == "record"
    assert status["computed_until"] == 200
    assert status["outcomes"]["status"] == "all_fallback"
    assert status["outcomes"]["fallback_count"] == len(router.records) > 0
    # Reading the status is observational and leaves the branch head unchanged.
    assert store.branch_reference("project", "run", "main").checkpoint_hash == head_before


def test_prepare_cli_warns_when_scenario_draft_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    small_benchmark_scenario,
    recording_router,
) -> None:
    source = tmp_path / "scenario.json"
    source.write_text(small_benchmark_scenario(30).canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_cli_accounted_router",
        lambda *_args, **_kwargs: recording_router(TruncatedProvider()),
    )
    arguments = [
        "prepare",
        "--scenario",
        str(source),
        "--generate-scenario",
        "--output",
        str(tmp_path / "draft.json"),
    ]

    assert cli.main(arguments) == 0

    captured = capsys.readouterr()
    preview = json.loads(captured.out)
    assert preview["generation_manifest"]["status"] == "rule_fallback"
    assert "warning: LLM fallback:" in captured.err and "invalid_output" in captured.err


def test_project_report_exists_before_the_run_is_marked_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, small_benchmark_scenario
) -> None:
    scenario = small_benchmark_scenario(30)
    store = ProjectStore(tmp_path)
    store.create_project("project")
    saved = store.save_scenario(
        "project", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
    )
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    TimelineStore(store, "project", "run").record(
        BranchRuntime.from_world(WorldState.from_scenario(scenario))
    )
    report_present_at_completion = []
    real_set_status = store.set_status

    def spy(project_id, run_id, status):
        if status == "completed":
            report = store._path(project_id, "runs", run_id, "branches", "main", "report.json")
            report_present_at_completion.append(report.exists())
        return real_set_status(project_id, run_id, status)

    monkeypatch.setattr(store, "set_status", spy)
    api = ProjectAPI(store, recover=False)
    try:
        api.submit("project", "run", "main", 30)
        # The single worker finishes the run before this no-op (#143).
        api._pool.submit(lambda: None).result(timeout=120)
    finally:
        api.close()

    # The UI fetches the report once when the status leaves "running".
    assert store.run("project", "run")["status"] == "completed"
    assert report_present_at_completion == [True]


def test_runtime_calls_leave_the_review_its_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, small_benchmark_scenario
) -> None:
    from sheltersim.llm.accounting import (
        ANALYSIS_RESERVED_CALLS,
        AccountedProvider,
        usage_limits,
    )
    from sheltersim.storage.usage import UsageLedger, task_campaign_ledger

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = ProjectStore(tmp_path / "projects")
    store.create_project("project")
    settings = LLMSettings(
        mode=LLMMode.RECORD, api_key="mock-key", max_api_calls=4, budget_usd=10.0
    )
    ledger = UsageLedger(store, "project", "cli-run-week")
    ledger.create(usage_limits(settings))
    router = LLMRouter(
        settings,
        provider=AccountedProvider(
            AcceptingProvider(),
            ledger,
            settings,
            "runtime",
            "main",
            campaign_ledger=task_campaign_ledger(store),
        ),
        tape=ResponseTape(),
    )

    run_scenario(small_benchmark_scenario(200), tmp_path / "run", llm_router=router)

    runtime = [record for record in router.records if record.stage == "runtime"]
    review = [record for record in router.records if record.stage == "review"]
    accepted_runtime = [record for record in runtime if record.outcome == "recorded"]
    assert len(accepted_runtime) == settings.max_api_calls - ANALYSIS_RESERVED_CALLS
    assert {record.outcome for record in runtime} - {"recorded"} == {"runtime_call_share_exhausted"}
    # Before the reserve, runtime decisions used every call and the review fell back.
    assert [record.outcome for record in review] == ["recorded"]
