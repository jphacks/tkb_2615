"""Offline CLI for profile preview, scenario preparation, and run inspection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from sheltersim import __version__
from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.comparison import compare_run_sets, render_comparison_markdown
from sheltersim.experiments.profiles import GeneratedProfileSet
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.query import InspectorQueryError, RunInspector
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.generation import generate_initial_needs
from sheltersim.llm.outcomes import fallback_warning, summarize_call_outcomes
from sheltersim.llm.prepare import generate_scenario_draft
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.report import review_saved_run


def _new_cli_account_id(scope: str) -> str:
    """Open a fresh ledger account so one exhausted allowance never silences later runs."""
    return f"cli-{scope}-{uuid.uuid4().hex}"


def _cli_usage_account_path(run_path: Path) -> Path:
    return run_path.parent / f".{run_path.name}.llm-usage-account.json"


def _cli_accounted_router(settings: LLMSettings, *, operation: str, account_id: str,
                          branch_id: str | None = None) -> LLMRouter:
    """Use one opaque ledger account throughout a prepared CLI Run workflow."""
    from sheltersim.llm.accounting import runtime_accounted_router, runtime_usage_store
    return runtime_accounted_router(
        runtime_usage_store(os.environ.get("SHELTERSIM_PROJECTS_DIR", "runs/.projects")),
        account_id=account_id,
        operation=operation,
        branch_id=branch_id,
        settings=settings,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the offline inspection command parser."""
    parser = argparse.ArgumentParser(
        prog="sheltersim",
        description="Shelter operations simulation tools.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    from sheltersim.storage.commands import configure_parser
    configure_parser(subparsers)

    subparsers.add_parser("usage", help="show bounded CLI and legacy-Demo LLM accounting receipts")

    catalog = subparsers.add_parser("model-catalog", help="show installed model equations, sources and limits")
    catalog.add_argument("--scenario", required=True, type=Path)
    evaluation = subparsers.add_parser("evaluate-models", help="run an explicitly requested offline paired model/parameter matrix")
    evaluation.add_argument("--scenario", required=True, type=Path)
    evaluation.add_argument("--plan", required=True, type=Path)
    evaluation.add_argument("--output", required=True, type=Path)
    evaluation.add_argument("--sources", type=Path)

    preview = subparsers.add_parser(
        "profiles-preview",
        help="display every profile and the generation manifest before a run",
    )
    preview.add_argument("path", type=Path, help="generated agents_initial.json artifact")

    prepare = subparsers.add_parser(
        "prepare",
        help="validate a scenario and preview every profile and planned event",
    )
    prepare.add_argument("--scenario", required=True, type=Path, help="ScenarioSpec JSON file")
    prepare.add_argument("--output", type=Path, help="write the preview JSON to this path")
    prepare.add_argument(
        "--freeze",
        action="store_true",
        help="mark this exact preview as explicitly frozen after review; requires --output",
    )
    generation = prepare.add_mutually_exclusive_group()
    generation.add_argument(
        "--generate-initial-needs",
        action="store_true",
        help="draft synthetic initial needs with Opus; output remains reviewable before freezing",
    )
    generation.add_argument(
        "--generate-scenario",
        action="store_true",
        help="draft all synthetic profiles and facility events with Opus for review",
    )

    run = subparsers.add_parser("run", help="run one frozen ScenarioSpec")
    run.add_argument("--offline", action="store_true", help="disable all APIs for this run")
    run.add_argument(
        "--llm-mode",
        choices=[mode.value for mode in LLMMode],
        help="override SHELTERSIM_LLM_MODE (auto uses Claude when an API key is configured)",
    )
    run.add_argument("--scenario", required=True, type=Path, help="ScenarioSpec or frozen preview")
    run.add_argument("--output", required=True, type=Path, help="run artifact directory")
    run.add_argument(
        "--behavior-policy",
        choices=("utility-softmax-v1", "priority-rule-v1"),
        help="swap one installed Agent action policy while preserving other scenario inputs",
    )
    run.add_argument(
        "--comparison-report",
        type=Path,
        help="optional validated comparison JSON to include in the Opus review",
    )
    run.add_argument(
        "--trace-level",
        choices=[level.value for level in TraceLevel],
        default=TraceLevel.SUMMARY.value,
    )

    inspect = subparsers.add_parser("inspect", help="query one run's saved JSON artifacts")
    inspect.add_argument("--run", required=True, type=Path, help="run artifact directory")
    query = inspect.add_mutually_exclusive_group(required=True)
    query.add_argument("--profiles", action="store_true", help="show every initial profile")
    query.add_argument("--events", action="store_true", help="show event records")
    query.add_argument("--agent", help="show one Agent's decision history")
    query.add_argument("--event", help="show one event's planned and realized records")
    query.add_argument("--cause", action="store_true", help="show an evidence-linked causal trace")
    inspect.add_argument(
        "--planned",
        action="store_true",
        help="with --events, show the pre-run event plan instead of realized outcomes",
    )
    inspect.add_argument(
        "--range",
        nargs=2,
        type=int,
        metavar=("START_MINUTE", "END_MINUTE"),
        help="inclusive decision-history minute range; requires --agent",
    )
    inspect.add_argument("--focus-agent", help="Agent ID to focus a --cause query")
    inspect.add_argument("--focus-event", help="event ID to focus a --cause query")

    compare = subparsers.add_parser(
        "compare", help="compare two saved runs by first KPI divergence and event evidence"
    )
    compare.add_argument(
        "--baseline", required=True, nargs="+", type=Path, help="one or more baseline run artifacts"
    )
    compare.add_argument(
        "--candidate", required=True, nargs="+", type=Path, help="paired candidate run artifacts"
    )
    compare.add_argument("--output", type=Path, help="write JSON and adjacent Markdown reports")
    compare.add_argument(
        "--behavior-policy-comparison",
        action="store_true",
        help=(
            "compare paired runs that differ only by a registered behavior policy; "
            "input equivalence is checked before KPI/action differences are reported"
        ),
    )

    review = subparsers.add_parser(
        "review", help="create or update an evidence-bound Opus review for a saved run"
    )
    review.add_argument("--run", required=True, type=Path, help="saved run artifact directory")
    review.add_argument(
        "--comparison-report", type=Path, help="optional comparison JSON for branch evidence"
    )
    review.add_argument("--offline", action="store_true", help="disable all API calls")
    review.add_argument(
        "--llm-mode",
        choices=[mode.value for mode in LLMMode],
        help="override SHELTERSIM_LLM_MODE",
    )

    web = subparsers.add_parser("web", help="serve a local read-only browser inspector")
    web.add_argument("--runs-dir", type=Path, default=Path("runs"), help="saved run directory")
    web.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback)")
    web.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    web.add_argument(
        "--demo-scenario",
        type=Path,
        help="optional local JSON scenario for the offline intervention demo",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one offline CLI action and return its process status."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "usage":
        from sheltersim.llm.accounting import runtime_usage_summary, runtime_usage_store
        print(_json_text(runtime_usage_summary(runtime_usage_store())))
        return 0
    if arguments.command == "project":
        from sheltersim.storage.commands import execute
        try:
            print(_json_text(execute(arguments)))
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(f"project operation failed: {exc}")
        return 0
    if arguments.command == "web":
        from sheltersim.web.server import serve_inspector

        serve_inspector(
            arguments.runs_dir,
            host=arguments.host,
            port=arguments.port,
            demo_scenario=arguments.demo_scenario,
        )
        return 0
    if arguments.command == "profiles-preview":
        try:
            profiles = GeneratedProfileSet.load(arguments.path)
        except (OSError, ValueError) as exc:
            parser.error(f"could not load profile artifact: {exc}")
        print(profiles.preview())
        return 0
    if arguments.command == "prepare":
        if arguments.freeze and arguments.output is None:
            parser.error("prepare --freeze requires --output so the frozen preview can be reviewed")
        if arguments.generate_scenario and arguments.output is None:
            parser.error("prepare --generate-scenario requires --output for review before freezing")
        if arguments.freeze and (arguments.generate_scenario or arguments.generate_initial_needs):
            parser.error("generated inputs must be previewed before a later --freeze run")
        try:
            source_document = json.loads(arguments.scenario.read_text(encoding="utf-8"))
            scenario = _load_scenario_input(arguments.scenario)
            router = None
            generation_manifest = None
            generated_scenario = None
            generation_calls: list[dict[str, Any]] = []
            if isinstance(source_document, dict):
                existing_manifest = source_document.get("generation_manifest")
                if isinstance(existing_manifest, dict):
                    generation_manifest = dict(existing_manifest)
                existing_generated = source_document.get("generated_scenario")
                if isinstance(existing_generated, dict):
                    generated_scenario = existing_generated
                existing_calls = source_document.get("llm_calls")
                if isinstance(existing_calls, list):
                    generation_calls = existing_calls
            if arguments.generate_scenario:
                account_id = _new_cli_account_id("prepare")
                router = _cli_accounted_router(LLMSettings.from_env(), operation="prepare", account_id=account_id)
                scenario, generation_manifest = generate_scenario_draft(scenario, router)
                generated_scenario = json.loads(scenario.canonical_json())
            if arguments.generate_initial_needs:
                account_id = _new_cli_account_id("prepare")
                router = _cli_accounted_router(LLMSettings.from_env(), operation="prepare", account_id=account_id)
                scenario = generate_initial_needs(scenario, router)
                generation_manifest = scenario.provenance.get("llm_initial_needs")
                generated_scenario = json.loads(scenario.canonical_json())
            preview_document = _scenario_preview(
                scenario,
                frozen=arguments.freeze,
                generation_manifest=generation_manifest,
                generated_scenario=generated_scenario,
            )
            if router is not None:
                generation_calls.extend(record.to_dict() for record in router.records)
            if router is not None:
                preview_document["llm_usage_account_id"] = account_id
            if generation_calls:
                preview_document["llm_calls"] = generation_calls
            output = _json_text(preview_document)
            if arguments.output is not None:
                _write_atomic(arguments.output, output + "\n")
            print(output)
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(f"could not prepare scenario: {exc}")
        return 0
    if arguments.command in {"model-catalog", "evaluate-models"}:
        from sheltersim.experiments.validation import EvaluationPlan, evidence_context, evaluate_models
        try:
            scenario = ScenarioSpec.load(arguments.scenario)
            if arguments.command == "model-catalog":
                result = evidence_context(scenario)
            else:
                plan = EvaluationPlan.model_validate(_read_json_object(arguments.plan))
                sources = _read_json_object(arguments.sources) if arguments.sources else None
                result = evaluate_models(scenario, plan, arguments.output, source_manifest=sources)
            print(_json_text(result))
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(f"model analysis failed: {exc}")
        return 0
    if arguments.command == "run":
        try:
            scenario = _load_frozen_scenario(arguments.scenario)
            if arguments.behavior_policy is not None:
                scenario = scenario.with_behavior_policy_variant(arguments.behavior_policy)
            settings = LLMSettings.from_env()
            if arguments.offline:
                settings = replace(settings, mode=LLMMode.OFFLINE)
            elif arguments.llm_mode is not None:
                settings = replace(settings, mode=LLMMode(arguments.llm_mode))
            # Each Run gets its own allowance under the current settings; the shared
            # campaign ledger still bounds total spending across all accounts.
            account_id = _new_cli_account_id("run")
            llm_router = _cli_accounted_router(settings, operation="runtime", account_id=account_id, branch_id="main")
            _write_atomic(_cli_usage_account_path(arguments.output), _json_text({"schema_version": 1, "account_id": account_id}) + "\n")
            comparison_report = (
                _read_json_object(arguments.comparison_report)
                if arguments.comparison_report is not None
                else None
            )
            world = run_scenario(
                scenario,
                arguments.output,
                trace_level=TraceLevel(arguments.trace_level),
                llm_router=llm_router,
                comparison_report=comparison_report,
            )
            llm_status = summarize_call_outcomes(llm_router.records)
            print(
                _json_text(
                    {
                        "run_directory": str(arguments.output),
                        "scenario_id": world.scenario_id,
                        "final_minute": world.minute_index,
                        "world_state_schema_version": world.schema_version,
                        "final_state_hash": hashlib.sha256(
                            world.canonical_json().encode("utf-8")
                        ).hexdigest(),
                        "llm_status": llm_status,
                    }
                )
            )
            warning = fallback_warning(llm_status)
            if warning is not None:
                print(f"warning: {warning}", file=sys.stderr)
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(f"could not run scenario: {exc}")
        return 0
    if arguments.command == "review":
        try:
            settings = LLMSettings.from_env()
            if arguments.offline:
                settings = replace(settings, mode=LLMMode.OFFLINE)
            elif arguments.llm_mode is not None:
                settings = replace(settings, mode=LLMMode(arguments.llm_mode))
            account_path = _cli_usage_account_path(arguments.run)
            account_document = json.loads(account_path.read_text(encoding="utf-8")) if account_path.exists() else {}
            account_id = account_document.get("account_id") if isinstance(account_document, dict) else None
            if not isinstance(account_id, str) or not account_id.startswith("cli-"):
                account_id = _new_cli_account_id("review")
            router = _cli_accounted_router(settings, operation="review", account_id=account_id, branch_id="main")
            comparison_report = (
                _read_json_object(arguments.comparison_report)
                if arguments.comparison_report is not None
                else None
            )
            result = review_saved_run(
                arguments.run,
                router,
                comparison_report=comparison_report,
            )
            print(
                _json_text(
                    {
                        "run_directory": str(arguments.run),
                        "review_status": result.status,
                        "input_hash": result.input_hash,
                        "review_path": str(arguments.run / "review.md"),
                    }
                )
            )
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(f"could not review run: {exc}")
        return 0
    if arguments.command == "compare":
        try:
            report = compare_run_sets(
                arguments.baseline,
                arguments.candidate,
                allow_behavior_policy_change=arguments.behavior_policy_comparison,
            )
            if arguments.output is not None:
                _write_atomic(arguments.output, _json_text(report) + "\n")
                markdown_path = arguments.output.with_suffix(".md")
                _write_atomic(markdown_path, render_comparison_markdown(report))
                print(_json_text({"report": str(arguments.output), "markdown": str(markdown_path)}))
            else:
                print(_json_text(report))
        except (OSError, ValueError) as exc:
            parser.error(f"could not compare runs: {exc}")
        return 0
    if arguments.command == "inspect":
        if arguments.planned and not arguments.events:
            parser.error("inspect --planned requires --events")
        if arguments.range is not None and arguments.agent is None:
            parser.error("inspect --range requires --agent")
        if (
            arguments.focus_agent is not None or arguments.focus_event is not None
        ) and not arguments.cause:
            parser.error("--focus-agent and --focus-event require --cause")
        if arguments.cause and (arguments.focus_agent is None) == (arguments.focus_event is None):
            parser.error("inspect --cause requires exactly one of --focus-agent or --focus-event")
        try:
            inspector = RunInspector(arguments.run)
            if arguments.profiles:
                result: Any = {"profiles": inspector.profiles()}
            elif arguments.events:
                result = {
                    "events": (
                        inspector.planned_events()
                        if arguments.planned
                        else inspector.realized_events()
                    ),
                    "event_type": "planned" if arguments.planned else "realized",
                }
            elif arguments.agent is not None:
                start_minute, end_minute = arguments.range or (None, None)
                result = {
                    "agent_id": arguments.agent,
                    "range": [start_minute, end_minute],
                    "decisions": inspector.agent_decisions(
                        arguments.agent,
                        start_minute=start_minute,
                        end_minute=end_minute,
                    ),
                }
            elif arguments.cause:
                result = inspector.causal_trace(
                    agent_id=arguments.focus_agent,
                    event_id=arguments.focus_event,
                )
            else:
                result = inspector.event(arguments.event)
            print(_json_text(result))
        except (OSError, InspectorQueryError, ValueError) as exc:
            parser.error(f"could not inspect run: {exc}")
        return 0
    parser.print_help()
    return 0


def _scenario_preview(
    scenario: ScenarioSpec,
    *,
    frozen: bool,
    generation_manifest: dict[str, Any] | None = None,
    generated_scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if generation_manifest is not None:
        generation_manifest = dict(generation_manifest)
        if generated_scenario is None:
            generated_scenario = json.loads(scenario.canonical_json())
        generation_manifest["manual_edits"] = _manual_edit_paths(
            generated_scenario, json.loads(scenario.canonical_json())
        )
        if frozen:
            generation_manifest["approved"] = True
            provenance = dict(scenario.provenance)
            provenance_key = (
                "llm_initial_needs"
                if "llm_initial_needs" in provenance
                else "llm_scenario_generation"
            )
            stable_keys = {
                "schema_version",
                "generator",
                "model_id",
                "prompt_version",
                "output_schema_version",
                "input_hash",
                "output_hash",
                "status",
                "scenario_source_hash",
                "generated_scenario_hash",
                "assumption",
                "manual_edits",
            }
            provenance[provenance_key] = {
                key: value for key, value in generation_manifest.items() if key in stable_keys
            }
            scenario = scenario.model_copy(update={"provenance": provenance})
    canonical_scenario = json.loads(scenario.canonical_json())
    document = {
        "preview_schema_version": 1,
        "frozen": frozen,
        "approval": {"approved": frozen, "method": "prepare --freeze" if frozen else None},
        "scenario_id": scenario.id,
        "scenario_hash": hashlib.sha256(scenario.canonical_json().encode("utf-8")).hexdigest(),
        "profile_count": len(scenario.profiles),
        "planned_event_count": len(scenario.scheduled_events),
        "scenario": canonical_scenario,
    }
    if generation_manifest is not None:
        document["generation_manifest"] = generation_manifest
    if generated_scenario is not None:
        document["generated_scenario"] = generated_scenario
    return document


def _manual_edit_paths(generated: dict[str, Any], frozen: dict[str, Any]) -> list[str]:
    """Describe changed generated fields without copying user-entered values into audit logs."""
    changed: list[str] = []
    ignored = {"provenance"}
    for key in sorted((set(generated) | set(frozen)) - ignored):
        if key == "profiles":
            before = {item.get("id"): item for item in generated.get("profiles", [])}
            after = {item.get("id"): item for item in frozen.get("profiles", [])}
            for index, profile_id in enumerate(sorted(before.keys() | after.keys())):
                left, right = before.get(profile_id, {}), after.get(profile_id, {})
                for field in sorted((set(left) | set(right)) - ignored):
                    if left.get(field) != right.get(field):
                        changed.append(f"profiles[{index}].{field}")
        elif generated.get(key) != frozen.get(key):
            changed.append(key)
    return changed


def _load_frozen_scenario(path: Path) -> ScenarioSpec:
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, dict) and document.get("preview_schema_version") == 1:
        if document.get("frozen") is not True:
            raise ValueError("scenario preview is not frozen; review it and run prepare --freeze")
        approval = document.get("approval")
        if not isinstance(approval, dict) or approval.get("approved") is not True:
            raise ValueError("scenario preview has no explicit prepare --freeze approval")
        frozen_scenario = document.get("scenario")
        if not isinstance(frozen_scenario, dict):
            raise ValueError("frozen preview does not contain a scenario object")
        expected_hash = document.get("scenario_hash")
        actual_hash = hashlib.sha256(
            ScenarioSpec.model_validate(frozen_scenario).canonical_json().encode("utf-8")
        ).hexdigest()
        if expected_hash != actual_hash:
            raise ValueError("frozen scenario hash does not match the approved scenario contents")
        return ScenarioSpec.model_validate(frozen_scenario)
    raise ValueError("run requires a reviewed frozen preview; use prepare --freeze first")


def _load_scenario_input(path: Path) -> ScenarioSpec:
    """Load a ScenarioSpec directly or its previously generated review preview."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, dict) and document.get("preview_schema_version") == 1:
        scenario_document = document.get("scenario")
        if not isinstance(scenario_document, dict):
            raise ValueError("scenario preview does not contain a scenario object")
        return ScenarioSpec.model_validate(scenario_document)
    return ScenarioSpec.from_json(json.dumps(document, ensure_ascii=False))


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def _write_atomic(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(contents, encoding="utf-8", newline="")
    temporary.replace(path)


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value
