"""Comparison reports name each side's psychology model and report movement and capacity waits."""

from __future__ import annotations

from sheltersim.domain.psychological_state import INTEGRATED_PSYCHOLOGY_POLICY_VERSION
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.experiments.comparison import render_comparison_markdown
from sheltersim.web.demo import DemoService


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
