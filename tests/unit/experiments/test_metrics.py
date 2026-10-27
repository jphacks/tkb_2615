"""KPI snapshots, run artifacts with their source IDs, and sanitization.

That the trace level changes only artifact detail over a Run is checked in
tests/integration/test_trace_level.py.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from sheltersim.core.engine import TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.staff import StaffService
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.metrics import MetricSnapshot
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter, _decision_document, _sanitize
from sheltersim.llm.report import _ASSUMPTION_LABELS, scenario_assumption_ids
from sheltersim.policies.behavior import UtilityBehaviorPolicy, WorldView
from sheltersim.policies.social import SocialPropagationParameters

FIXTURE = Path(__file__).parents[3] / "fixtures" / "scenario_100_agents.json"
BASELINE_PATH = Path(__file__).parents[3] / "fixtures" / "scenarios" / "baseline_100_7d.json"


def load_scenario() -> ScenarioSpec:
    return ScenarioSpec.load(FIXTURE)


def test_metric_snapshot_reports_values_and_source_ids_without_mutation() -> None:
    scenario = load_scenario()
    world = WorldState.from_scenario(scenario)
    world.minute_index = 4
    world.agents["A001"].stress = 0.8
    world.metric_state.update(
        {
            "toilet_wait_count": 2.0,
            "toilet_wait_total_minutes": 7.0,
            "toilet_wait_max_minutes": 5.0,
        }
    )
    staff_service = StaffService(world, task_duration_minutes=6, unresolved_after_minutes=20)
    staff_service.request("A001", minute=4)
    staff_service.assign_pending(4)
    assigned_task_id = world.staff["S01"].active_task_id
    before = world.canonical_json()

    metrics = MetricSnapshot.from_world(world, minute=4)
    source_ids = json.loads(metrics.source_ids_json)

    assert metrics.toilet_wait_mean_minutes == 3.5
    assert metrics.stress_mean == 0.008
    assert metrics.stress_high_count == 1
    assert metrics.staff_assigned_task_count == 1
    assert metrics.staff_busy_count == 1
    assert metrics.staff_load_ratio == 0.2
    assert metrics.unresolved_support_request_count == 1
    assert source_ids["high_stress_agents"] == ["A001"]
    assert source_ids["unresolved_support_agents"] == ["A001"]
    assert source_ids["staff_tasks"] == [assigned_task_id]
    assert world.canonical_json() == before


def test_support_wait_metrics_explain_busy_qualified_staff_and_elapsed_wait() -> None:
    world = WorldState.from_scenario(load_scenario())
    world.staff = {"S01": world.staff["S01"]}
    service = StaffService(world, task_duration_minutes=10, unresolved_after_minutes=20)
    first = service.request("A001")
    assert service.assign_pending(0) == (first.request_id,)

    world.minute_index = 4
    waiting = service.request("A002", minute=4)
    assert service.assign_pending(4) == ()
    snapshot = MetricSnapshot.from_world(world)
    sources = json.loads(snapshot.source_ids_json)

    assert sources["support_wait_by_agent"]["A002"] == {
        "request_id": waiting.request_id,
        "waited_minutes": 0,
        "reason_codes": ["staff_busy"],
    }
    world.minute_index = 7
    later = MetricSnapshot.from_world(world)
    later_sources = json.loads(later.source_ids_json)
    assert later_sources["support_wait_by_agent"]["A002"]["waited_minutes"] == 3


def test_artifact_writer_emits_required_files_and_redacts_sensitive_fields(
    tmp_path: Path, monkeypatch
) -> None:
    scenario = load_scenario()
    scenario.model_configs.setdefault("artifact-test", {})["api_key"] = "never-write-this"
    world = WorldState.from_scenario(scenario)
    world.schedule_event(
        ScheduledEvent(
            event_id="email-event",
            kind=EventKind.EXOGENOUS,
            due_minute=0,
            priority=0,
            sequence=world.allocate_event_sequence(),
            event_type="unregistered",
            payload={"email": "resident@example.test", "kind": "toilet"},
        )
    )
    EventDispatcher().dispatch_due(world, 0)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-secret")
    artifacts = RunArtifactWriter(scenario, tmp_path / "run", trace_level=TraceLevel.FULL)

    artifacts.write_initial(WorldState.from_scenario(scenario))
    artifacts.capture_metric(world, minute=0)
    artifacts.finalize(world)

    assert {
        "manifest.json",
        "agents_initial.json",
        "events_planned.json",
        "events_realized.jsonl",
        "decisions.jsonl",
        "metrics.csv",
    } <= {path.name for path in (tmp_path / "run").iterdir()}
    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_status"] == "completed"
    assert manifest["execution_mode"] == "offline"
    assert manifest["run_id"]
    assert manifest["model_configs"]["artifact-test"]["api_key"] == "[REDACTED]"
    initial_profiles = json.loads(
        (tmp_path / "run" / "agents_initial.json").read_text(encoding="utf-8")
    )["profiles"]
    planned_events = json.loads(
        (tmp_path / "run" / "events_planned.json").read_text(encoding="utf-8")
    )["events"]
    assert len(initial_profiles) == 100
    assert len(planned_events) == len(scenario.scheduled_events)
    event_line = (tmp_path / "run" / "events_realized.jsonl").read_text(encoding="utf-8")
    assert '"email":"[REDACTED]"' in event_line
    assert "email-event" not in json.dumps(planned_events)
    all_outputs = "".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "run").iterdir() if path.is_file()
    )
    assert "never-write-this" not in all_outputs
    assert "environment-secret" not in all_outputs
    assert "resident@example.test" not in all_outputs
    with (tmp_path / "run" / "metrics.csv").open(encoding="utf-8", newline="") as source:
        metric_rows = list(csv.DictReader(source))
    assert len(metric_rows) == 1
    assert metric_rows[0]["minute_index"] == "0"
    assert "email-event" in metric_rows[0]["source_ids_json"]


def test_run_manifest_and_decision_trace_include_source_ids(tmp_path: Path) -> None:
    scenario = ScenarioSpec.load(BASELINE_PATH)
    world = WorldState.from_scenario(scenario)
    output = tmp_path / "run"
    writer = RunArtifactWriter(scenario, output, trace_level=TraceLevel.OFF)
    writer.write_initial(world, execution_mode="offline")
    run_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    model_assumptions = run_manifest["model_assumptions"]

    assert model_assumptions["behavior"]["source_id"] == "ASM-UTILITY-BEHAVIOR-V1"
    assert model_assumptions["psychology"]["source_id"] == "ASM-H04-CAUSAL-STRESS-V1"
    assert model_assumptions["staff_support"]["source_id"] == "ASM-STAFF-SUPPORT-LIFECYCLE-V1"
    assert model_assumptions["toilet_service"]["source_id"] == "ASM-TOILET-SERVICE-DURATION-V1"
    assert model_assumptions["social_propagation"]["source_id"] == "ASM-SOCIAL-PROPAGATION-V1"
    assert (
        model_assumptions["event_triggered_social_limits"]["source_id"]
        == "ASM-EVENT-TRIGGERED-SOCIAL-LIMITS-V1"
    )
    assert (
        model_assumptions["event_triggered_support_priority_limits"]["source_id"]
        == "ASM-SUPPORT-PRIORITY-LIMITS-V1"
    )
    assert model_assumptions["zone_routing"]["source_id"] == "ASM-ZONE-ROUTING-V1"
    assert model_assumptions["inspector_zone_projection"]["affects_simulation"] is False
    assert model_assumptions["social_propagation"]["coefficients"] == {
        key: value
        for key, value in asdict(SocialPropagationParameters()).items()
        if key != "source_id"
    }
    assumption_ids = {
        item["source_id"]
        for item in model_assumptions.values()
        if item.get("source_type") == "assumption" and "source_id" in item
    }
    assert assumption_ids <= _ASSUMPTION_LABELS.keys()

    proposal = UtilityBehaviorPolicy(temperature=0.2, decision_interval_minutes=60).propose(
        WorldView.from_world(world), "A001", CounterRNG(world.seed)
    )
    trace = _decision_document(proposal, full=False)
    assert trace["source_type"] == "assumption"
    assert trace["source_id"] == "ASM-UTILITY-BEHAVIOR-V1"

    priority_scenario = scenario.with_behavior_policy_variant("priority-rule-v1")
    priority_world = WorldState.from_scenario(priority_scenario)
    priority_writer = RunArtifactWriter(
        priority_scenario, tmp_path / "priority-run", trace_level=TraceLevel.OFF
    )
    priority_writer.write_initial(priority_world, execution_mode="offline")
    priority_manifest = json.loads(
        (tmp_path / "priority-run" / "manifest.json").read_text(encoding="utf-8")
    )
    assert (
        priority_manifest["model_assumptions"]["behavior"]["source_id"]
        == "ASM-PRIORITY-RULE-BEHAVIOR-V1"
    )
    assert "ASM-PRIORITY-RULE-BEHAVIOR-V1" in scenario_assumption_ids(priority_scenario)

    controller = OfflineSimulationController(world, writer)
    route = controller.space.route("sleeping", "toilet", "A001")
    assert route is not None
    assert controller._schedule_travel(
        agent_id="A001",
        route=route,
        purpose="toilet_request",
        facility_id="toilet_1",
        minute=0,
        validate_state=False,
    )
    travel_event = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "agent_travel_complete"
    )
    assert travel_event.provenance["source_id"] == "ASM-ZONE-ROUTING-V1"


def test_artifact_sanitizer_preserves_usage_and_hides_secrets() -> None:
    sanitized = _sanitize(
        {
            "input_tokens": 24,
            "output_tokens": 3,
            "estimated_cost_usd": 0.0001,
            "access_token": "private",
            "api_key": "private",
        }
    )

    assert sanitized == {
        "input_tokens": 24,
        "output_tokens": 3,
        "estimated_cost_usd": 0.0001,
        "access_token": "[REDACTED]",
        "api_key": "[REDACTED]",
    }
