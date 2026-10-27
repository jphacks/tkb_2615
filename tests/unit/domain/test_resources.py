"""Opt-in ResourceLedger accounting, reservation, replay and artifact contracts."""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TraceLevel
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.resources import RESOURCE_LEDGER_KEY
from sheltersim.events.handlers import (
    EventDispatcher,
    ResourceLedgerHandler,
)
from sheltersim.events.models import EventApplicationError, EventKind, EventStatus, ScheduledEvent
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.query import RunInspector
from sheltersim.web.inspector import InspectorService
from sheltersim.web.server import _dispatch_api

ROOT = Path(__file__).parents[1]
SCENARIO_FIXTURE = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"


def scenario_with_stock(
    *,
    water_ml: int = 0,
    food_meal_units: int = 0,
    source_ids: list[str] | None = None,
    scheduled_events: list[dict[str, object]] | None = None,
    duration_minutes: int = 5,
) -> ScenarioSpec:
    document = json.loads(SCENARIO_FIXTURE.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = scheduled_events or []
    for scheduled in document["scheduled_events"]:
        if scheduled.get("event_type", "").startswith("resource_"):
            scheduled["payload"] = {
                "warehouse_id": "warehouse-main",
                **scheduled.get("payload", {}),
            }
    document["initial_stock"] = {
        "schema_version": 1,
        "warehouse_id": "warehouse-main",
        "zone_id": "sleeping",
        "water_ml": water_ml,
        "food_meal_units": food_meal_units,
        "source_ids": (
            source_ids
            if source_ids is not None
            else (["ASM-H01-TEST-INITIAL-STOCK-V1"] if water_ml or food_meal_units else [])
        ),
    }
    return ScenarioSpec.model_validate(document)


def event(
    event_id: str,
    operation: str,
    payload: dict[str, object],
    *,
    sequence: int = 0,
    due_minute: int = 0,
    source_id: str = "ASM-H01-TEST-RESOURCE-OPERATION-V1",
) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=event_id,
        kind=EventKind.EXOGENOUS,
        due_minute=due_minute,
        priority=0,
        sequence=sequence,
        event_type=f"resource_{operation}",
        payload={"warehouse_id": "warehouse-main", **payload},
        source_id=source_id,
    )


def resource_dispatcher() -> EventDispatcher:
    return EventDispatcher(
        {
            f"resource_{operation}": ResourceLedgerHandler(operation)
            for operation in ("delivery", "loss", "reserve", "release", "consume")
        }
    )


def test_resource_ledger_is_opt_in_strict_and_world_checkpoint_roundtrips() -> None:
    old_fixture = ScenarioSpec.load(SCENARIO_FIXTURE)
    assert old_fixture.schema_version == 3
    baseline_world = WorldState.from_scenario(old_fixture)
    assert baseline_world.resources == {}
    assert RESOURCE_LEDGER_KEY not in baseline_world.model_state

    scenario = scenario_with_stock(water_ml=1500, food_meal_units=3)
    world = WorldState.from_scenario(scenario)
    world.validate()
    assert world.schema_version == 5
    assert world.resources == {"water_ml": 1500, "food_meal_units": 3}
    assert world.model_state[RESOURCE_LEDGER_KEY]["schema_version"] == 1
    assert world.model_state[RESOURCE_LEDGER_KEY]["warehouse_id"] == "warehouse-main"
    assert world.model_state[RESOURCE_LEDGER_KEY]["zone_id"] == "sleeping"

    restored = WorldState.from_json(world.canonical_json())
    assert restored.canonical_json() == world.canonical_json()
    metrics = MetricsRecorder()
    metrics.capture(world, minute=0)
    checkpoint = Checkpoint.from_json(Checkpoint.capture(world, metrics=metrics).to_json())
    assert checkpoint.schema_version == 1
    assert checkpoint.world_state_schema_version == 5
    assert checkpoint.restore_world().canonical_json() == world.canonical_json()
    assert checkpoint.restore_metrics().to_dict() == metrics.to_dict()
    assert checkpoint.metrics_state["schema_version"] == 4
    assert checkpoint.metrics_state["resource_snapshots"][0]["food_meal_units_initial"] == 3

    legacy_row = asdict(metrics.snapshots[0])
    for key in (
        "support_no_qualified_worker_count",
        "support_no_qualified_worker_request_minutes",
        "support_fairness_promoted_assignment_count",
    ):
        legacy_row.pop(key)
    legacy_metrics = MetricsRecorder.from_dict(
        {"schema_version": 1, "snapshots": [legacy_row]}
    )
    assert legacy_metrics.to_dict() == {
        "schema_version": 4,
        "snapshots": [asdict(metrics.snapshots[0])],
    }


def test_checkpoint_restore_continuation_preserves_ledger_audit_and_kpis() -> None:
    original = WorldState.from_scenario(scenario_with_stock(food_meal_units=2))
    original.schedule_event(
        event(
            "reserve-before-checkpoint",
            "reserve",
            {
                "resource_id": "food_meal_units",
                "reservation_id": "meal-A001",
                "agent_id": "A001",
                "quantity": 2,
            },
        )
    )
    resource_dispatcher().dispatch_due(original, 0)
    original_metrics = MetricsRecorder()
    original_metrics.capture(original, minute=0)
    checkpoint = Checkpoint.capture(original, metrics=original_metrics)

    restored = checkpoint.restore_world()
    restored_metrics = checkpoint.restore_metrics()
    for world in (original, restored):
        world.minute_index = 1
        world.schedule_event(
            event(
                "consume-after-checkpoint",
                "consume",
                {
                    "resource_id": "food_meal_units",
                    "reservation_id": "meal-A001",
                    "agent_id": "A001",
                    "quantity": 2,
                },
                sequence=world.allocate_event_sequence(),
                due_minute=1,
            )
        )
        resource_dispatcher().dispatch_due(world, 1)
    original_metrics.capture(original, minute=1)
    restored_metrics.capture(restored, minute=1)

    assert restored.canonical_json() == original.canonical_json()
    assert restored.event_log.records == original.event_log.records
    assert restored.model_state[RESOURCE_LEDGER_KEY] == original.model_state[RESOURCE_LEDGER_KEY]
    assert restored_metrics.to_dict() == original_metrics.to_dict()
    final_account = restored.model_state[RESOURCE_LEDGER_KEY]["accounts"]["food_meal_units"]
    assert final_account["initial"] - final_account["served"] == final_account["remaining"] == 0
    assert final_account["reserved"] == 0


def test_resource_scenario_v2_migration_and_v3_stock_rejections() -> None:
    source = json.loads(SCENARIO_FIXTURE.read_text(encoding="utf-8"))
    assert source["schema_version"] == 2
    migrated = ScenarioSpec.from_json(json.dumps(source))
    assert migrated.schema_version == 3
    assert migrated.initial_stock is None
    canonical = json.loads(migrated.canonical_json())
    assert canonical["schema_version"] == 3
    assert "initial_stock" not in canonical

    for version in (1, 2):
        old_input = dict(source, schema_version=version, initial_stock={"water_ml": 1})
        with pytest.raises(ValueError, match="only valid in ScenarioSpec v3"):
            ScenarioSpec.from_json(json.dumps(old_input))
    with pytest.raises(ValidationError, match="schema_version"):
        ScenarioSpec.model_validate(source)

    with pytest.raises(ValidationError, match="source_id"):
        scenario_with_stock(water_ml=1, source_ids=[])
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        scenario_with_stock(food_meal_units=-1)


def test_resource_operations_balance_inventory_and_preserve_audit_sources() -> None:
    world = WorldState.from_scenario(
        scenario_with_stock(
            water_ml=1000,
            food_meal_units=2,
            source_ids=["ASM-H01-TEST-INITIAL-STOCK-V1"],
        )
    )
    events = [
        event("delivery-1", "delivery", {"resource_id": "water_ml", "quantity": 500}, sequence=0),
        event("loss-1", "loss", {"resource_id": "water_ml", "quantity": 100}, sequence=1),
        event(
            "reserve-1",
            "reserve",
            {
                "resource_id": "food_meal_units",
                "reservation_id": "meal-A001",
                "agent_id": "A001",
                "quantity": 2,
            },
            sequence=2,
        ),
        event(
            "consume-1",
            "consume",
            {
                "resource_id": "food_meal_units",
                "reservation_id": "meal-A001",
                "agent_id": "A001",
                "quantity": 1,
            },
            sequence=3,
        ),
        event(
            "release-1",
            "release",
            {
                "resource_id": "food_meal_units",
                "reservation_id": "meal-A001",
                "agent_id": "A001",
            },
            sequence=4,
        ),
    ]
    for scheduled in events:
        world.schedule_event(scheduled)

    outcomes = resource_dispatcher().dispatch_due(world, 0)

    assert all(outcome.status is EventStatus.APPLIED for outcome in outcomes)
    ledger = world.model_state[RESOURCE_LEDGER_KEY]
    water = ledger["accounts"]["water_ml"]
    food = ledger["accounts"]["food_meal_units"]
    assert water == {
        "initial": 1000,
        "delivered": 500,
        "served": 0,
        "losses": 100,
        "remaining": 1400,
        "reserved": 0,
        "source_ids": [
            "ASM-H01-TEST-INITIAL-STOCK-V1",
            "ASM-H01-TEST-RESOURCE-OPERATION-V1",
        ],
        "reservations": {},
    }
    assert (
        food["initial"] + food["delivered"] - food["served"] - food["losses"] == food["remaining"]
    )
    assert food["remaining"] == 1
    assert food["reserved"] == 0
    assert food["reservations"] == {}
    assert [entry["event_id"] for entry in ledger["audit"][2:]] == [
        scheduled.event_id for scheduled in events
    ]
    assert ledger["audit"][-1]["source_id"] == "ASM-H01-TEST-RESOURCE-OPERATION-V1"
    assert ledger["audit"][-1]["warehouse_id"] == "warehouse-main"
    assert ledger["audit"][-1]["remaining"] == food["remaining"]
    assert ledger["audit"][-1]["reserved"] == food["reserved"]
    world.validate()


def test_invalid_and_cross_agent_resource_actions_are_atomic() -> None:
    world = WorldState.from_scenario(scenario_with_stock(food_meal_units=1))
    handler = ResourceLedgerHandler("reserve")
    before = world.canonical_json()
    with pytest.raises(EventApplicationError, match="positive integer"):
        ResourceLedgerHandler("delivery").apply(
            world,
            event("negative-delivery", "delivery", {"resource_id": "water_ml", "quantity": -1}),
        )
    assert world.canonical_json() == before

    with pytest.raises(EventApplicationError, match="unknown resource ID"):
        handler.apply(
            world,
            event(
                "unknown-resource",
                "reserve",
                {
                    "resource_id": "unknown",
                    "reservation_id": "reserve-unknown",
                    "agent_id": "A001",
                    "quantity": 1,
                },
            ),
        )
    assert world.canonical_json() == before

    with pytest.raises(EventApplicationError, match="unknown resource warehouse ID"):
        ResourceLedgerHandler("delivery").apply(
            world,
            event(
                "unknown-warehouse",
                "delivery",
                {
                    "warehouse_id": "warehouse-unknown",
                    "resource_id": "water_ml",
                    "quantity": 1,
                },
            ),
        )
    assert world.canonical_json() == before

    handler.apply(
        world,
        event(
            "reserve-food",
            "reserve",
            {
                "resource_id": "food_meal_units",
                "reservation_id": "meal-A001",
                "agent_id": "A001",
                "quantity": 1,
            },
        ),
    )
    reserved = world.canonical_json()
    with pytest.raises(EventApplicationError, match="different Agent"):
        ResourceLedgerHandler("consume").apply(
            world,
            event(
                "cross-agent-consume",
                "consume",
                {
                    "resource_id": "food_meal_units",
                    "reservation_id": "meal-A001",
                    "agent_id": "A002",
                    "quantity": 1,
                },
            ),
        )
    assert world.canonical_json() == reserved

    consume = ResourceLedgerHandler("consume")
    consume.apply(
        world,
        event(
            "consume-once",
            "consume",
            {
                "resource_id": "food_meal_units",
                "reservation_id": "meal-A001",
                "agent_id": "A001",
                "quantity": 1,
            },
        ),
    )
    consumed = world.canonical_json()
    with pytest.raises(EventApplicationError, match="unknown resource reservation"):
        consume.apply(
            world,
            event(
                "consume-twice",
                "consume",
                {
                    "resource_id": "food_meal_units",
                    "reservation_id": "meal-A001",
                    "agent_id": "A001",
                    "quantity": 1,
                },
            ),
        )
    assert world.canonical_json() == consumed


def test_one_last_meal_reservation_is_stable_by_event_sequence() -> None:
    world = WorldState.from_scenario(scenario_with_stock(food_meal_units=1))
    world.schedule_event(
        event(
            "reserve-A002",
            "reserve",
            {
                "resource_id": "food_meal_units",
                "reservation_id": "meal-A002",
                "agent_id": "A002",
                "quantity": 1,
            },
            sequence=1,
        )
    )
    world.schedule_event(
        event(
            "reserve-A001",
            "reserve",
            {
                "resource_id": "food_meal_units",
                "reservation_id": "meal-A001",
                "agent_id": "A001",
                "quantity": 1,
            },
            sequence=0,
        )
    )
    results = resource_dispatcher().dispatch_due(world, 0)

    assert [record.event.event_id for record in results] == ["reserve-A001", "reserve-A002"]
    assert [record.status for record in results] == [EventStatus.APPLIED, EventStatus.FAILED]
    food = world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["food_meal_units"]
    assert food["reserved"] == 1
    assert food["reservations"]["meal-A001"]["agent_id"] == "A001"
    assert food["reservations"]["meal-A001"]["quantity"] == 1
    assert "meal-A002" not in food["reservations"]
    world.validate()


def test_resource_metrics_logs_and_state_are_trace_level_independent(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {
            "id": "resource-reserve",
            "due_minute": 0,
            "sequence": 0,
            "event_type": "resource_reserve",
            "source_id": "ASM-H01-TEST-RESOURCE-ALLOCATION-V1",
            "payload": {
                "warehouse_id": "warehouse-main",
                "resource_id": "food_meal_units",
                "reservation_id": "meal-A001",
                "agent_id": "A001",
                "quantity": 1,
            },
        },
        {
            "id": "resource-consume",
            "due_minute": 0,
            "sequence": 1,
            "event_type": "resource_consume",
            "source_id": "ASM-H01-TEST-RESOURCE-SERVICE-V1",
            "payload": {
                "warehouse_id": "warehouse-main",
                "resource_id": "food_meal_units",
                "reservation_id": "meal-A001",
                "agent_id": "A001",
                "quantity": 1,
            },
        },
    ]
    scenario = scenario_with_stock(
        water_ml=900,
        food_meal_units=2,
        scheduled_events=events,
        duration_minutes=3,
    )
    off_directory = tmp_path / "trace-off"
    summary_directory = tmp_path / "trace-summary"
    off_world = run_scenario(scenario, off_directory, trace_level=TraceLevel.OFF)
    summary_world = run_scenario(scenario, summary_directory, trace_level=TraceLevel.SUMMARY)

    assert off_world.canonical_json() == summary_world.canonical_json()
    assert (off_directory / "events_realized.jsonl").read_bytes() == (
        summary_directory / "events_realized.jsonl"
    ).read_bytes()
    assert (off_directory / "metrics.csv").read_bytes() == (
        summary_directory / "metrics.csv"
    ).read_bytes()
    with (off_directory / "metrics.csv").open(encoding="utf-8", newline="") as stream:
        metric_rows = list(csv.DictReader(stream))
    assert metric_rows[0]["food_meal_units_initial"] == "2"
    assert metric_rows[0]["food_meal_units_served"] == "1"
    assert metric_rows[0]["food_meal_units_remaining"] == "1"
    off_manifest = json.loads((off_directory / "manifest.json").read_text(encoding="utf-8"))
    summary_manifest = json.loads((summary_directory / "manifest.json").read_text(encoding="utf-8"))
    assert off_manifest["resource_ledger_summary"] == summary_manifest["resource_ledger_summary"]
    assert off_manifest["resource_ledger_summary"]["audit_entry_count"] == 4
    assert off_manifest["initial_stock"]["source_ids"] == ["ASM-H01-TEST-INITIAL-STOCK-V1"]
    assert (off_directory / "resource_ledger.json").is_file()
    ledger = json.loads((off_directory / "resource_ledger.json").read_text(encoding="utf-8"))
    assert ledger == off_world.model_state[RESOURCE_LEDGER_KEY]
    assert ledger["warehouse_id"] == "warehouse-main"
    query = RunInspector(off_directory).resource_ledger()
    assert query == ledger
    run_id = off_manifest["run_id"]
    runs_directory = tmp_path / "runs"
    runs_directory.mkdir()
    shutil.copytree(off_directory, runs_directory / run_id)
    web_inspector = InspectorService(runs_directory)
    assert web_inspector.resource_ledger(run_id) == {"enabled": True, "ledger": ledger}
    assert _dispatch_api(web_inspector, f"/api/v1/runs/{run_id}/resource-ledger", {}) == {
        "enabled": True,
        "ledger": ledger,
    }
    assert (
        web_inspector.overview(run_id)["manifest"]["resource_ledger_summary"]["zone_id"]
        == "sleeping"
    )


def test_world_rejects_resource_accounting_or_reservation_corruption() -> None:
    world = WorldState.from_scenario(scenario_with_stock(food_meal_units=1))
    world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["food_meal_units"]["remaining"] = 2

    with pytest.raises(StateInvariantError, match="accounting does not balance"):
        world.validate()


def test_resource_contract_rejects_unknown_zone_resource_keys_and_audit_tampering() -> None:
    with pytest.raises(ValidationError, match="references unknown zone"):
        scenario = scenario_with_stock(food_meal_units=1).model_dump(mode="json")
        scenario["initial_stock"]["zone_id"] = "unknown-zone"
        ScenarioSpec.model_validate(scenario)

    invalid_zone_world = WorldState.from_scenario(scenario_with_stock(food_meal_units=1))
    invalid_zone_world.model_state[RESOURCE_LEDGER_KEY]["zone_id"] = "unknown-zone"
    with pytest.raises(StateInvariantError, match="zone_id is invalid or unknown"):
        invalid_zone_world.validate()
    with pytest.raises(StateInvariantError, match="zone_id is invalid or unknown"):
        Checkpoint.capture(invalid_zone_world)

    world = WorldState.from_scenario(scenario_with_stock(food_meal_units=1))
    world.resources["unknown_resource"] = 1
    with pytest.raises(StateInvariantError, match="exactly the ledger resource IDs"):
        world.validate()

    world.resources.pop("unknown_resource")
    world.model_state[RESOURCE_LEDGER_KEY]["audit"][0]["remaining"] += 1
    with pytest.raises(StateInvariantError, match="audit after-state does not match replay"):
        world.validate()
