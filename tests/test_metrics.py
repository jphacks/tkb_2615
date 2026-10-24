"""KPI snapshots, run artifacts, sanitization, and observation neutrality."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from sheltersim.core.engine import TickEngine, TickPhase, TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.domain.staff import StaffService
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.metrics import MetricSnapshot
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.policies.behavior import BehaviorDecisionRunner, WorldView

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


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


def test_trace_level_changes_artifact_detail_not_simulation_hash(tmp_path: Path) -> None:
    scenario = load_scenario()

    def run(level: TraceLevel) -> tuple[str, list[str]]:
        world = WorldState.from_scenario(scenario)
        output_directory = tmp_path / level.value
        artifacts = RunArtifactWriter(scenario, output_directory, trace_level=level)
        artifacts.write_initial(world)
        policy_runner = BehaviorDecisionRunner()

        def observe(phase, state, minute, rng) -> None:
            if phase is TickPhase.PROPOSE_ACTIONS:
                for proposal in policy_runner.propose_due(WorldView.from_world(state), rng):
                    artifacts.record_decision(proposal)
            if phase is TickPhase.COLLECT_METRICS:
                artifacts.capture_metric(state, minute=minute)

        traces: list[object] = []
        engine = TickEngine(
            world,
            phase_handler=observe,
            trace_level=level,
            trace_sink=traces.append if level is not TraceLevel.OFF else None,
        )
        engine.run(steps=2)
        final_state = world.canonical_json()
        artifacts.finalize(world)
        decisions = (output_directory / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        return final_state, decisions

    off_state, off_decisions = run(TraceLevel.OFF)
    summary_state, summary_decisions = run(TraceLevel.SUMMARY)
    full_state, full_decisions = run(TraceLevel.FULL)

    assert off_state == summary_state == full_state
    assert off_decisions == []
    assert summary_decisions
    assert "candidates" not in json.loads(summary_decisions[0])
    assert "candidates" in json.loads(full_decisions[0])
