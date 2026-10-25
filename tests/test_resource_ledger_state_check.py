"""Operations and per-tick checks use the ledger's current state; the full check replays it."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.resources import (
    RESOURCE_LEDGER_KEY,
    ResourceLedgerError,
    apply_resource_operation,
    validate_resource_ledger,
    validate_resource_ledger_state,
)
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenarios" / "h00_small_replay.json"
SOURCE = "ASM-TEST-RESOURCE-OPERATION"


def _world() -> WorldState:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = 30
    document["scheduled_events"] = []
    document["initial_stock"] = {
        "schema_version": 1,
        "warehouse_id": "warehouse-main",
        "zone_id": "sleeping",
        "water_ml": 1_000,
        "food_meal_units": 4,
        "source_ids": ["ASM-TEST-INITIAL-STOCK"],
    }
    return WorldState.from_scenario(ScenarioSpec.model_validate(document))


def _apply(world: WorldState, minute: int, operation: str, **payload: object) -> None:
    world.minute_index = minute
    sequence = len(world.model_state[RESOURCE_LEDGER_KEY]["audit"])
    apply_resource_operation(
        world,
        ScheduledEvent(
            event_id=f"op-{sequence:03d}",
            kind=EventKind.EXOGENOUS,
            due_minute=minute,
            priority=0,
            sequence=sequence,
            event_type=f"resource_{operation}",
            payload={"warehouse_id": "warehouse-main", **payload},
            source_id=SOURCE,
        ),
        operation,
    )


def _operated_world() -> WorldState:
    world = _world()
    agent_id = sorted(world.agents)[0]
    meal = {"resource_id": "food_meal_units", "agent_id": agent_id}
    _apply(world, 1, "delivery", resource_id="water_ml", quantity=500)
    _apply(world, 2, "reserve", reservation_id="r-1", quantity=2, **meal)
    _apply(world, 3, "consume", reservation_id="r-1", quantity=1, **meal)
    _apply(world, 4, "reserve", reservation_id="r-2", quantity=1, **meal)
    _apply(world, 5, "release", reservation_id="r-2", **meal)
    _apply(world, 9, "loss", resource_id="water_ml", quantity=200)
    world.minute_index = 20
    return world


def _check_state(world: WorldState) -> None:
    validate_resource_ledger_state(
        world.model_state[RESOURCE_LEDGER_KEY],
        world.resources,
        set(world.agents),
        set(world.zones),
        minute_index=world.minute_index,
    )


def _check_full(world: WorldState) -> None:
    validate_resource_ledger(
        world.model_state[RESOURCE_LEDGER_KEY], world.resources, set(world.agents), set(world.zones)
    )


def test_operations_keep_both_checks_passing() -> None:
    world = _operated_world()

    _check_state(world)
    _check_full(world)
    assert world.resources == {"water_ml": 1_300, "food_meal_units": 3}
    assert world.model_state[RESOURCE_LEDGER_KEY]["used_reservation_ids"] == ["r-1", "r-2"]


def test_a_successful_operation_leaves_the_previous_ledger_untouched() -> None:
    world = _operated_world()
    previous = world.model_state[RESOURCE_LEDGER_KEY]
    snapshot = copy.deepcopy(previous)

    _apply(world, 20, "delivery", resource_id="food_meal_units", quantity=3)

    assert previous == snapshot
    assert world.model_state[RESOURCE_LEDGER_KEY] is not previous
    assert len(world.model_state[RESOURCE_LEDGER_KEY]["audit"]) == len(previous["audit"]) + 1


def test_a_rejected_operation_changes_nothing() -> None:
    world = _operated_world()
    before = world.canonical_json()

    with pytest.raises(ResourceLedgerError, match="reservation ID 'r-1' was already used"):
        _apply(
            world,
            20,
            "reserve",
            resource_id="water_ml",
            reservation_id="r-1",
            agent_id=sorted(world.agents)[0],
            quantity=1,
        )

    assert world.canonical_json() == before


def test_the_newest_audit_record_must_match_its_account() -> None:
    world = _operated_world()
    world.model_state[RESOURCE_LEDGER_KEY]["audit"][-1]["remaining"] += 1

    with pytest.raises(ResourceLedgerError, match="balance does not match audit"):
        _check_state(world)
    with pytest.raises(StateInvariantError, match="balance does not match audit"):
        world.validate(validate_event_history=False)


def test_an_old_audit_record_is_left_to_the_full_check() -> None:
    world = _operated_world()
    delivery = world.model_state[RESOURCE_LEDGER_KEY]["audit"][2]
    assert delivery["operation"] == "delivery" and delivery["minute"] < world.minute_index - 1
    delivery["quantity"] += 1

    _check_state(world)
    world.validate(validate_event_history=False)
    with pytest.raises(StateInvariantError, match="after-state does not match replay"):
        world.validate()


def test_the_full_check_rejects_a_reused_reservation_id() -> None:
    world = _operated_world()
    ledger = world.model_state[RESOURCE_LEDGER_KEY]
    second_reserve = next(row for row in ledger["audit"] if row["reservation_id"] == "r-2")
    second_reserve["reservation_id"] = "r-1"

    with pytest.raises(ResourceLedgerError, match="reservation ID was reused"):
        _check_full(world)
