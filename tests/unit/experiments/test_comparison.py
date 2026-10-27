"""Comparison reports: first divergence, causal links, paired-seed summaries, each side's
psychology model, and movement and capacity waits.

Comparisons of saved Runs are checked in tests/integration/test_comparison.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.psychological_state import INTEGRATED_PSYCHOLOGY_POLICY_VERSION
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.experiments.branch import AddToiletCommand, BranchRuntime
from sheltersim.experiments.comparison import (
    build_causal_trace,
    compare_branches,
    compare_run_sets,
    render_comparison_markdown,
    summarize_seed_comparisons,
)
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.web.demo import DemoService

FIXTURE = Path(__file__).parents[3] / "fixtures" / "scenario_100_agents.json"


def test_comparison_markdown_has_no_warning_for_the_research_model() -> None:
    markdown = render_comparison_markdown(
        {
            "psychology_models": {
                "baseline": INTEGRATED_PSYCHOLOGY_POLICY_VERSION,
                "candidate": INTEGRATED_PSYCHOLOGY_POLICY_VERSION,
            },
            "final_metric_differences": {},
        }
    )
    assert f"baseline `{INTEGRATED_PSYCHOLOGY_POLICY_VERSION}`" in markdown
    assert "Warning" not in markdown


def test_comparison_reports_movement_and_capacity_wait_kpis_with_event_evidence(
    small_replay_scenario, route_edit_command
) -> None:
    demo = DemoService(small_replay_scenario())
    demo.dispatch("start")
    demo.dispatch("pause", {"branch_id": "baseline"})
    result = demo.dispatch(
        "layout-branch",
        {
            "source_branch_id": "baseline",
            "idempotency_key": "route-kpi-test",
            "commands": [route_edit_command()],
        },
    )
    baseline = demo.branches["baseline"]
    candidate = demo.branches[result["branch_id"]]
    world = candidate.runtime.world
    travel_sequence = world.allocate_event_sequence()
    travel_event = ScheduledEvent(
        event_id="route-kpi-travel",
        kind=EventKind.COMPLETION,
        due_minute=0,
        priority=0,
        sequence=travel_sequence,
        event_type="agent_travel_complete",
        payload={
            "agent_id": "A001",
            "travel_minutes": 8,
            "route_edge_ids": ["corridor_toilet"],
        },
        source_id="route-kpi-travel",
        provenance={"source_type": "assumption", "source_id": "ASM-A05-ROUTE-EDIT-V1"},
    )
    world.event_log.append(
        EventLogRecord(
            event=travel_event,
            realized_minute=0,
            status=EventStatus.APPLIED,
        )
    )
    wait_sequence = world.allocate_event_sequence()
    wait_event = ScheduledEvent(
        event_id="route-kpi-capacity-wait",
        kind=EventKind.ENDOGENOUS,
        due_minute=0,
        priority=0,
        sequence=wait_sequence,
        event_type="zone_edge_capacity_wait",
        payload={"agent_id": "A002", "edge_id": "corridor_toilet"},
        source_id="ASM-A05-ROUTE-EDIT-V1",
        provenance={"source_type": "assumption", "source_id": "ASM-A05-ROUTE-EDIT-V1"},
    )
    world.event_log.append(
        EventLogRecord(
            event=wait_event,
            realized_minute=0,
            status=EventStatus.SUPPRESSED,
            detail="route edge capacity is full",
        )
    )
    baseline.status = candidate.status = "completed"

    report = demo._comparison_payload("baseline", baseline, result["branch_id"], candidate)

    assert report["routing_metrics"]["travel_minutes_total"] == {
        "baseline": 0,
        "candidate": 8,
        "delta": 8,
    }
    assert report["routing_metrics"]["capacity_wait_observation_count"]["delta"] == 1
    evidence = report["routing_evidence"]["candidate"]
    assert evidence["completed_travel_events"][0]["event_id"] == "route-kpi-travel"
    assert evidence["completed_travel_events"][0]["source_id"] == "ASM-A05-ROUTE-EDIT-V1"
    assert evidence["capacity_wait_events"][0]["event_id"] == "route-kpi-capacity-wait"
    assert evidence["capacity_wait_events"][0]["edge_id"] == "corridor_toilet"


def test_noop_branch_report_has_zero_divergence() -> None:
    parent = BranchRuntime.from_world(WorldState.from_scenario(ScenarioSpec.load(FIXTURE)))
    parent.metrics.capture(parent.world, minute=0)
    left = parent.fork("left")
    right = parent.fork("right")

    report = compare_branches(left, right).to_dict()

    assert report["first_divergence_minute"] is None
    assert report["changed_agents"] == []
    assert report["changed_facilities"] == []
    assert report["added_event_ids"] == []
    assert report["no_op_equivalent"] is True


def test_intervention_report_explains_first_kpi_and_state_difference() -> None:
    parent = BranchRuntime.from_world(WorldState.from_scenario(ScenarioSpec.load(FIXTURE)))
    baseline = parent.fork("baseline")
    candidate = parent.fork("candidate")
    baseline.metrics.capture(baseline.world, minute=0)
    candidate.apply(AddToiletCommand("extra-toilet", "toilet", capacity=2))
    candidate.metrics.capture(candidate.world, minute=0)

    report = compare_branches(baseline, candidate).to_dict()

    assert report["first_divergence_minute"] == 0
    assert report["changed_metrics_at_first_divergence"]["toilet_working_units"]["delta"] == 2
    assert report["changed_facilities"][0]["id"] == "extra-toilet"
    assert report["added_event_ids"] == ["branch-intervention-0"]
    assert "single seed" in report["interpretation"]


def test_paired_seed_summary_reports_distribution_without_causal_claim() -> None:
    summary = summarize_seed_comparisons(
        [
            {"seed": 1, "final_metric_differences": {"stress_mean": {"delta": 0.1}}},
            {"seed": 2, "final_metric_differences": {"stress_mean": {"delta": 0.3}}},
            {"seed": 3, "final_metric_differences": {"stress_mean": {"delta": 0.2}}},
        ]
    )

    assert summary["seed_count"] == 3
    assert summary["paired_delta_summary"]["stress_mean"]["mean"] == pytest.approx(0.2)
    assert summary["paired_delta_summary"]["stress_mean"]["median"] == pytest.approx(0.2)
    assert summary["paired_delta_summary"]["stress_mean"]["p95"] > 0.2
    assert "not a guarantee" in summary["interpretation"]


def test_run_sets_reject_duplicate_or_unpaired_seeds(tmp_path: Path) -> None:
    scenario = ScenarioSpec.model_validate(
        json.loads(FIXTURE.read_text(encoding="utf-8")) | {"duration_minutes": 3}
    )
    world = WorldState.from_scenario(scenario)
    writer = RunArtifactWriter(scenario, tmp_path / "run")
    writer.write_initial(world)
    writer.capture_metric(world, minute=0)
    writer.finalize(world)

    with pytest.raises(ValueError, match="duplicate seed"):
        compare_run_sets([tmp_path / "run", tmp_path / "run"], [tmp_path / "run"])
    other_scenario = ScenarioSpec.model_validate(
        json.loads(FIXTURE.read_text(encoding="utf-8"))
        | {"duration_minutes": 3, "seed": scenario.seed + 1}
    )
    other_world = WorldState.from_scenario(other_scenario)
    other_writer = RunArtifactWriter(other_scenario, tmp_path / "other-seed")
    other_writer.write_initial(other_world)
    other_writer.capture_metric(other_world, minute=0)
    other_writer.finalize(other_world)
    with pytest.raises(ValueError, match="same seeds"):
        compare_run_sets([tmp_path / "run"], [tmp_path / "other-seed"])


def test_causal_trace_connects_facility_event_queue_wait_agent_and_staff() -> None:
    decisions = [
        {
            "minute_index": 10,
            "agent_id": "A001",
            "action": "request_toilet",
            "target_id": "toilet_1",
        },
        {
            "minute_index": 11,
            "agent_id": "A002",
            "action": "seek_help",
            "target_id": "support-request-2",
        },
    ]
    events = [
        {
            "realized_minute": 5,
            "status": "applied",
            "event": {
                "event_id": "outage-1",
                "event_type": "facility_outage",
                "payload": {"facility_id": "toilet_1", "units_out": 1},
                "provenance": {},
            },
        },
        {
            "realized_minute": 19,
            "status": "applied",
            "event": {
                "event_id": "toilet-complete-4",
                "event_type": "toilet_service_complete",
                "payload": {
                    "facility_id": "toilet_1",
                    "agent_id": "A001",
                    "task_id": "toilet-service-4",
                },
                "source_id": "toilet-service-4",
                "causation_id": "toilet-queue-A001",
                "provenance": {"requested_at": 10, "queue_wait_minutes": 8},
            },
        },
        {
            "realized_minute": 20,
            "status": "applied",
            "event": {
                "event_id": "staff-complete-7",
                "event_type": "staff_support_complete",
                "payload": {
                    "request_id": "support-request-2",
                    "agent_id": "A002",
                    "staff_id": "S01",
                    "task_id": "staff-task-7",
                },
                "source_id": "staff-task-7",
                "causation_id": "support-request-2",
                "provenance": {},
            },
        },
    ]

    trace = build_causal_trace(decisions, events, focus_agent_id="A001")
    nodes = {node["id"]: node for node in trace["nodes"]}
    edges = {(edge["source"], edge["target"], edge["relationship"]) for edge in trace["edges"]}

    assert nodes["toilet-request:toilet_1:A001:10"]["details"]["queue_wait_minutes"] == 8
    assert (
        "event:outage-1",
        "toilet-request:toilet_1:A001:10",
        "capacity_context",
    ) in edges
    assert ("agent:A001", "decision:10:A001:0", "made_by") in edges
