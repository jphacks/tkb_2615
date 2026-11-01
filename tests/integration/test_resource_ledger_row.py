"""resource_ledger_v1 owned by Rust, with its operations (docs/rust-port/lanes/m4-resources.md).

At every operation of the gate and the 7-day Run, benchmarks/rule_shadow/shadow_resources.py
compares the kernel with the Python rule. Here, the branches those Runs may not reach: each
operation against the Python rule bit for bit, random sequences with refusals, what taints the row,
and that the check takes nothing the Python rules refuse.
"""

from __future__ import annotations

import copy
import json
import random
from types import SimpleNamespace
from typing import Any

import pytest
from support.paths import SMALL_REPLAY

from sheltersim import native
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.checkpoint_cache import canonical_bytes
from sheltersim.core.state_mirror import rust_validated
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain import resources
from sheltersim.domain.resources import (
    RESOURCE_LEDGER_KEY as KEY,
)
from sheltersim.domain.resources import (
    ResourceLedgerError,
    apply_resource_operation,
    reservation_id_was_used,
    resource_metric_values,
    validate_resource_ledger,
    validate_resource_ledger_state,
)
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.scenario import ScenarioSpec

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

SOURCE = "ASM-TEST-RESOURCE-OPERATION"
SEQUENCE = [0]


def _world() -> WorldState:
    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = 300
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


def _event(operation: str, source: Any = SOURCE, event_id: Any = None, **payload: Any) -> Any:
    SEQUENCE[0] += 1
    return ScheduledEvent(
        event_id=event_id or f"op-{SEQUENCE[0]}",
        kind=EventKind.EXOGENOUS,
        due_minute=0,
        priority=0,
        sequence=SEQUENCE[0],
        event_type=f"resource_{operation}",
        payload={"warehouse_id": "warehouse-main", **payload},
        source_id=source,
    )


def _kernel() -> Any:
    return native.rules("resources")


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _stand_in(world: WorldState) -> SimpleNamespace:
    return SimpleNamespace(
        model_state={KEY: json.loads(json.dumps(world.model_state[KEY]))},
        resources=dict(world.resources),
        agents=world.agents,
        zones=world.zones,
        minute_index=world.minute_index,
    )


def _both(world: WorldState, event: Any, operation: str) -> None:
    """The kernel must run, and write what the Python rule writes on a copy, to the bit."""
    stand_in = _stand_in(world)
    apply_resource_operation.__wrapped__(stand_in, event, operation)
    _kernel().apply_operation(world, event, operation)
    assert _text(world.model_state[KEY]) == _text(stand_in.model_state[KEY])
    assert _text(world.resources) == _text(stand_in.resources)
    _assert_row_is_the_tree(world)


def _assert_row_is_the_tree(world: WorldState) -> None:
    mirror = world._core.mirror(KEY)
    assert mirror.tainted is None
    assert mirror.verify()
    assert mirror.row_json() == canonical_bytes(world.model_state[KEY])


def _operate(world: WorldState) -> str:
    agent = sorted(world.agents)[0]
    meal = {"resource_id": "food_meal_units", "agent_id": agent}
    world.minute_index = 1
    _both(world, _event("delivery", resource_id="water_ml", quantity=500), "delivery")
    world.minute_index = 2
    _both(world, _event("reserve", reservation_id="r-1", quantity=2, **meal), "reserve")
    world.minute_index = 3
    _both(world, _event("consume", reservation_id="r-1", quantity=1, **meal), "consume")
    world.minute_index = 4
    _both(world, _event("reserve", reservation_id="r-2", quantity=1, **meal), "reserve")
    _both(world, _event("release", reservation_id="r-2", **meal), "release")
    world.minute_index = 9
    _both(world, _event("loss", resource_id="water_ml", quantity=200), "loss")
    return agent


def test_each_operation_is_the_python_rule_to_the_bit() -> None:
    world = _world()
    _operate(world)
    account = world.model_state[KEY]["accounts"]["food_meal_units"]
    assert account["reservations"]["r-1"]["quantity"] == 1
    assert world.model_state[KEY]["used_reservation_ids"] == ["r-1", "r-2"]
    assert world.resources == {"water_ml": 1_300, "food_meal_units": 3}
    validate_resource_ledger(
        world.model_state[KEY], world.resources, set(world.agents), set(world.zones)
    )


def test_a_consume_of_the_whole_reservation_and_a_new_source() -> None:
    world = _world()
    agent = _operate(world)
    meal = {"resource_id": "food_meal_units", "agent_id": agent}
    _both(world, _event("consume", reservation_id="r-1", quantity=1, **meal), "consume")
    assert world.model_state[KEY]["accounts"]["food_meal_units"]["reservations"] == {}
    _both(
        world,
        _event("delivery", source="ASM-ANOTHER-SOURCE", resource_id="water_ml", quantity=1),
        "delivery",
    )
    assert world.model_state[KEY]["accounts"]["water_ml"]["source_ids"][-1] == "ASM-ANOTHER-SOURCE"
    assert world.model_state[KEY]["audit"][-1]["source_ids"][-1] == "ASM-ANOTHER-SOURCE"


def test_the_namespace_is_not_written_whole_and_the_resources_are_a_new_dict() -> None:
    world = _world()
    mirror = world._core.mirror(KEY)
    tree = world.model_state[KEY]
    whole_writes = mirror.stats()["whole_writes"]
    held = world.resources
    before = dict(held)
    _operate(world)
    assert mirror.stats()["whole_writes"] == whole_writes
    assert world.model_state[KEY] is tree
    assert world.resources is not held
    assert held == before


def test_the_public_rule_runs_the_kernel() -> None:
    world = _world()
    world.minute_index = 1
    tree = world.model_state[KEY]
    apply_resource_operation(
        world, _event("delivery", resource_id="water_ml", quantity=5), "delivery"
    )
    assert world.model_state[KEY] is tree


REFUSED = {
    "an unknown warehouse": (
        lambda agent: ScheduledEvent(
            event_id="bad-warehouse",
            kind=EventKind.EXOGENOUS,
            due_minute=0,
            priority=0,
            sequence=90_001,
            event_type="resource_delivery",
            payload={"warehouse_id": "elsewhere", "resource_id": "water_ml", "quantity": 1},
            source_id=SOURCE,
        ),
        "delivery",
        "unknown resource warehouse",
    ),
    "an unknown resource": (
        lambda agent: _event("delivery", resource_id="soap", quantity=1),
        "delivery",
        "unknown resource ID",
    ),
    "a quantity of zero": (
        lambda agent: _event("delivery", resource_id="water_ml", quantity=0),
        "delivery",
        "must be a positive integer",
    ),
    "a bool quantity": (
        lambda agent: _event("delivery", resource_id="water_ml", quantity=True),
        "delivery",
        "must be a positive integer",
    ),
    "a float quantity": (
        lambda agent: _event("delivery", resource_id="water_ml", quantity=1.0),
        "delivery",
        "must be a positive integer",
    ),
    "no source": (
        lambda agent: _event("delivery", source=None, resource_id="water_ml", quantity=1),
        "delivery",
        "require a source_id",
    ),
    "other payload fields": (
        lambda agent: _event("delivery", resource_id="water_ml", quantity=1, extra=1),
        "delivery",
        "payload fields do not match",
    ),
    "a loss past what is free": (
        lambda agent: _event("loss", resource_id="food_meal_units", quantity=4),
        "loss",
        "exceeds unreserved",
    ),
    "a used ID": (
        lambda agent: _event(
            "reserve",
            resource_id="food_meal_units",
            agent_id=agent,
            reservation_id="r-1",
            quantity=1,
        ),
        "reserve",
        "was already used",
    ),
    "a reservation past what is free": (
        lambda agent: _event(
            "reserve",
            resource_id="food_meal_units",
            agent_id=agent,
            reservation_id="r-9",
            quantity=9,
        ),
        "reserve",
        "exceeds available",
    ),
    "a bad reservation ID": (
        lambda agent: _event(
            "reserve",
            resource_id="food_meal_units",
            agent_id=agent,
            reservation_id="r 9",
            quantity=1,
        ),
        "reserve",
        "must be a stable ID",
    ),
    "someone unknown": (
        lambda agent: _event(
            "reserve",
            resource_id="food_meal_units",
            agent_id="nobody",
            reservation_id="r-9",
            quantity=1,
        ),
        "reserve",
        "unknown resource Agent ID",
    ),
    "a reservation not there": (
        lambda agent: _event(
            "release", resource_id="food_meal_units", agent_id=agent, reservation_id="r-2"
        ),
        "release",
        "unknown resource reservation",
    ),
    "a consume past the reservation": (
        lambda agent: _event(
            "consume",
            resource_id="food_meal_units",
            agent_id=agent,
            reservation_id="r-1",
            quantity=2,
        ),
        "consume",
        "exceeds the reservation",
    ),
    "an unknown operation": (
        lambda agent: _event("delivery", resource_id="water_ml", quantity=1),
        "steal",
        "unknown resource operation",
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_refusal_changes_nothing_and_has_the_python_text(case: str) -> None:
    build, operation, text = REFUSED[case]
    world = _world()
    agent = _operate(world)
    event = build(agent)
    before = world.canonical_json()
    with pytest.raises(native.Unsupported):
        _kernel().apply_operation(world, event, operation)
    with pytest.raises(ResourceLedgerError, match=text):
        apply_resource_operation(world, event, operation)
    assert world.canonical_json() == before
    _assert_row_is_the_tree(world)


def test_someone_else_s_reservation_has_the_python_text() -> None:
    world = _world()
    _operate(world)
    other = sorted(world.agents)[1]
    event = _event("release", resource_id="food_meal_units", agent_id=other, reservation_id="r-1")
    with pytest.raises(native.Unsupported):
        _kernel().apply_operation(world, event, "release")
    with pytest.raises(ResourceLedgerError, match="belongs to a different Agent"):
        apply_resource_operation(world, event, "release")


@pytest.mark.parametrize(
    ("source", "event_id", "text"),
    [
        ("not a stable id", "op-a", "source_ids are invalid"),
        (SOURCE, "bad id", "audit IDs are invalid"),
        ("x" * 65, "op-b", "source_ids are invalid"),
    ],
)
def test_a_record_python_would_turn_down_goes_to_python(
    source: str, event_id: str, text: str
) -> None:
    world = _world()
    world.minute_index = 1
    # ScheduledEvent itself refuses these IDs, so only another object can bring them.
    event = SimpleNamespace(
        payload={"warehouse_id": "warehouse-main", "resource_id": "water_ml", "quantity": 1},
        source_id=source,
        event_id=event_id,
    )
    before = world.canonical_json()
    with pytest.raises(native.Unsupported):
        _kernel().apply_operation(world, event, "delivery")
    with pytest.raises(ResourceLedgerError, match=text):
        apply_resource_operation(world, event, "delivery")
    assert world.canonical_json() == before


def test_an_older_minute_goes_to_python() -> None:
    world = _world()
    _operate(world)
    world.minute_index = 4
    with pytest.raises(native.Unsupported):
        _kernel().apply_operation(
            world, _event("delivery", resource_id="water_ml", quantity=1), "delivery"
        )


def test_random_sequences_with_refusals_match_the_python_rule() -> None:
    randomizer = random.Random(20261101)
    ran = refused = 0
    for _ in range(25):
        world = _world()
        people = sorted(world.agents)[:3]
        reservations = [f"r-{index}" for index in range(12)]
        for minute in range(1, 60):
            world.minute_index = minute
            operation = randomizer.choice(["delivery", "loss", "reserve", "release", "consume"])
            payload: dict[str, Any] = {
                "resource_id": randomizer.choice(["water_ml", "food_meal_units"])
            }
            if operation != "release":
                payload["quantity"] = randomizer.choice([1, 1, 2, 3, 50, 2000])
            if operation in {"reserve", "release", "consume"}:
                payload["agent_id"] = randomizer.choice(people)
                payload["reservation_id"] = randomizer.choice(reservations)
            event = _event(
                operation, source=randomizer.choice([SOURCE, "ASM-SECOND", "ASM-THIRD"]), **payload
            )
            stand_in = _stand_in(world)
            before = world.canonical_json()
            try:
                apply_resource_operation.__wrapped__(stand_in, event, operation)
                error = None
            except ResourceLedgerError as raised:
                error = raised
            try:
                _kernel().apply_operation(world, event, operation)
            except native.Unsupported:
                assert error is not None, f"the kernel left to Python what it accepts: {event}"
                assert world.canonical_json() == before
                refused += 1
                continue
            assert error is None, f"the kernel took what Python refused: {error}"
            ran += 1
            assert _text(world.model_state[KEY]) == _text(stand_in.model_state[KEY])
            assert _text(world.resources) == _text(stand_in.resources)
        _assert_row_is_the_tree(world)
    assert ran > 200
    assert refused > 200


def test_the_comparison_sees_a_change_of_one_unit() -> None:
    world = _world()
    agent = _operate(world)
    stand_in = _stand_in(world)
    apply_resource_operation.__wrapped__(
        stand_in,
        _event(
            "consume",
            resource_id="food_meal_units",
            agent_id=agent,
            reservation_id="r-1",
            quantity=1,
        ),
        "consume",
    )
    assert _text(stand_in.model_state[KEY]) != _text(world.model_state[KEY])


def test_the_metric_columns_and_the_used_ids() -> None:
    world = _world()
    _operate(world)
    assert _kernel().metric_values(world) == resource_metric_values.__wrapped__(world)
    assert list(_kernel().metric_values(world)) == list(resource_metric_values.__wrapped__(world))
    assert reservation_id_was_used(world, "r-1") is True
    assert reservation_id_was_used(world, "r-404") is False
    # The Python rule answers for anything but a str, as `in` on a list does.
    assert reservation_id_was_used(world, 3) is False


def test_no_ledger_has_no_columns() -> None:
    world = _world()
    del world.model_state[KEY]
    assert resource_metric_values(world) is None
    assert _kernel().metric_values(world) is None


def _validate_state(world: WorldState) -> None:
    validate_resource_ledger_state(
        world.model_state[KEY],
        world.resources,
        set(world.agents),
        set(world.zones),
        minute_index=world.minute_index,
    )


# name -> what it does, and the text the Python rules say.
TAMPERING = {
    "remaining": (
        lambda world: world.model_state[KEY]["accounts"]["water_ml"].__setitem__("remaining", 1),
        "accounting does not balance",
    ),
    "a negative balance": (
        lambda world: world.model_state[KEY]["accounts"]["water_ml"].__setitem__("losses", -1),
        "must be non-negative",
    ),
    "resources": (
        lambda world: world.resources.__setitem__("water_ml", 1),
        "does not match ledger remaining",
    ),
    "a whole assignment of resources": (
        lambda world: setattr(world, "resources", {"water_ml": 1}),
        "exactly the ledger resource IDs",
    ),
    "a newest audit record": (
        lambda world: world.model_state[KEY]["audit"][-1].__setitem__("remaining", 7),
        "balance does not match audit",
    ),
    "a repeated reservation ID": (
        lambda world: world.model_state[KEY]["used_reservation_ids"].append("r-1"),
        "must be unique",
    ),
    "a reservation ID not a str": (
        lambda world: world.model_state[KEY]["used_reservation_ids"].append(3),
        "reservation IDs are invalid",
    ),
    "a reservation's quantity": (
        lambda world: world.model_state[KEY]["accounts"]["food_meal_units"]["reservations"][
            "r-1"
        ].__setitem__("quantity", 5),
        "reserved total does not match",
    ),
    "a reservation's person": (
        lambda world: world.model_state[KEY]["accounts"]["food_meal_units"]["reservations"][
            "r-1"
        ].__setitem__("agent_id", "nobody"),
        "reservation identity is invalid",
    ),
    "the schema": (
        lambda world: world.model_state[KEY].__setitem__("schema_version", 2),
        "unsupported resource ledger schema",
    ),
    "the warehouse": (
        lambda world: world.model_state[KEY].__setitem__("warehouse_id", "bad id"),
        "warehouse_id is invalid",
    ),
    "the zone": (
        lambda world: world.model_state[KEY].__setitem__("zone_id", "nowhere"),
        "zone_id is invalid or unknown",
    ),
    "a source": (
        lambda world: world.model_state[KEY]["accounts"]["water_ml"]["source_ids"].append("bad id"),
        "source_ids are invalid",
    ),
    "a repeated source": (
        lambda world: world.model_state[KEY]["accounts"]["water_ml"]["source_ids"].append(SOURCE),
        "source_ids must be unique",
    ),
    "an extra field": (
        lambda world: world.model_state[KEY].__setitem__("extra", 1),
        "fields do not match v1",
    ),
    "a whole namespace": (
        lambda world: world.model_state.__setitem__(KEY, {"schema_version": 1}),
        "fields do not match v1",
    ),
}


@pytest.mark.parametrize("case", sorted(TAMPERING))
def test_each_write_is_stopped_with_the_python_text(case: str) -> None:
    tamper, text = TAMPERING[case]
    world = _world()
    _operate(world)
    assert rust_validated(world, KEY)
    world.validate(validate_event_history=False)
    tamper(world)
    assert not rust_validated(world, KEY)
    with pytest.raises(StateInvariantError, match=text):
        world.validate(validate_event_history=False)
    with pytest.raises(ResourceLedgerError, match=text):
        resource_metric_values(world)
    with pytest.raises(ResourceLedgerError, match=text):
        apply_resource_operation(
            world, _event("delivery", resource_id="water_ml", quantity=1), "delivery"
        )


def test_balances_that_add_up_but_are_not_the_audit_trail_s_are_refused() -> None:
    world = _world()
    _operate(world)
    account = world.model_state[KEY]["accounts"]["water_ml"]
    account["served"] += 1
    account["remaining"] -= 1
    world.resources["water_ml"] -= 1
    assert not rust_validated(world, KEY)
    with pytest.raises(ResourceLedgerError, match="balance does not match audit"):
        _validate_state(world)


def test_a_bool_in_the_resources_is_refused() -> None:
    world = _world()
    _operate(world)
    world.resources["food_meal_units"] = True
    assert not rust_validated(world, KEY)
    with pytest.raises(ResourceLedgerError, match="does not match ledger remaining"):
        _validate_state(world)


def test_a_record_of_an_older_minute_goes_to_python_and_passes_there() -> None:
    """Python's check of a tick reads only the newest records, so it says nothing of this one."""
    world = _world()
    _operate(world)
    ledger = world.model_state[KEY]
    newest = dict(ledger["audit"][-1])
    ledger["audit"].append({**newest, "minute": 0, "source_ids": list(newest["source_ids"])})
    world.minute_index = 30
    assert not rust_validated(world, KEY)
    _validate_state(world)


def test_a_write_past_the_table_is_read_by_the_python_rules() -> None:
    world = _world()
    _operate(world)
    good = json.loads(json.dumps(world.model_state[KEY]))
    good["accounts"]["water_ml"]["remaining"] = 1
    dict.__setitem__(world.model_state, KEY, good)
    assert not rust_validated(world, KEY)
    with pytest.raises(native.Unsupported):
        _kernel().metric_values(world)
    with pytest.raises(native.Unsupported):
        _kernel().apply_operation(
            world, _event("delivery", resource_id="water_ml", quantity=1), "delivery"
        )
    with pytest.raises(native.Unsupported):
        _kernel().reservation_id_was_used(world, "r-1")
    with pytest.raises(StateInvariantError, match="accounting does not balance"):
        world.validate()


def test_a_tainted_row_comes_back_with_the_python_rule_s_write() -> None:
    world = _world()
    _operate(world)
    world.model_state[KEY]["audit"][2]["served"] = 1.5
    assert world._core.mirror(KEY).tainted is not None
    mirror = world._core.mirror(KEY)
    world.model_state[KEY]["audit"][2]["served"] = 0
    assert mirror.refit()
    _assert_row_is_the_tree(world)


def test_a_forwarded_append_keeps_the_row() -> None:
    world = _world()
    _operate(world)
    record = dict(world.model_state[KEY]["audit"][-1])
    world.model_state[KEY]["audit"].append(
        {**record, "minute": 9, "event_id": "by-hand", "operation": "delivery", "agent_id": None}
    )
    _assert_row_is_the_tree(world)


def test_a_deep_copy_and_a_restore_have_their_own_rows() -> None:
    world = _world()
    _operate(world)
    copied = copy.deepcopy(world)
    copied.minute_index = 10
    before = world.canonical_json()
    _both(copied, _event("delivery", resource_id="water_ml", quantity=1), "delivery")
    assert world.canonical_json() == before
    _assert_row_is_the_tree(world)

    restored = WorldState.from_dict(json.loads(world.canonical_json()))
    assert rust_validated(restored, KEY)
    restored.minute_index = 10
    _both(restored, _event("delivery", resource_id="water_ml", quantity=1), "delivery")
    checkpoint = Checkpoint.capture(restored)
    assert (
        checkpoint.state_hash
        == Checkpoint.capture(
            WorldState.from_dict(json.loads(restored.canonical_json()))
        ).state_hash
    )


def test_the_save_check_still_replays_the_audit_trail() -> None:
    """The kernel answers for a tick; only the replay sees a change that left the newest alone."""
    world = _world()
    _operate(world)
    world.validate(validate_event_history=False)
    world.model_state[KEY]["audit"][2]["delivered"] += 1
    with pytest.raises(StateInvariantError, match="audit after-state does not match replay"):
        world.validate()


def test_the_check_takes_nothing_python_refuses_on_random_writes() -> None:
    randomizer = random.Random(7)
    values: list[Any] = [-1, 0, 1, 2, 3, 500, 1_300, 10**30, True, 1.0, None, "x", "bad id", "r-1"]
    accepted = refused = 0
    for _ in range(600):
        world = _world()
        _operate(world)
        ledger = world.model_state[KEY]
        account = ledger["accounts"][randomizer.choice(["water_ml", "food_meal_units"])]
        target = randomizer.randrange(9)
        value = randomizer.choice(values)
        if target == 0:
            account[randomizer.choice(list(resources._BALANCE_FIELDS))] = value
        elif target == 1:
            world.resources[randomizer.choice(["water_ml", "food_meal_units"])] = value
        elif target == 2:
            ledger["used_reservation_ids"].append(value)
        elif target == 3:
            ledger["accounts"]["food_meal_units"]["reservations"]["r-1"][
                randomizer.choice(["reservation_id", "resource_id", "agent_id", "quantity"])
            ] = value
        elif target == 4:
            account["source_ids"].append(value)
        elif target == 5:
            ledger["audit"][-1][randomizer.choice(list(resources._AUDIT_FIELDS))] = value
        elif target == 6:
            ledger[randomizer.choice(["schema_version", "warehouse_id", "zone_id"])] = value
        elif target == 7:
            ledger["accounts"]["food_meal_units"]["reservations"][str(value)] = {
                "reservation_id": str(value),
                "resource_id": "food_meal_units",
                "agent_id": sorted(world.agents)[0],
                "quantity": 1,
            }
        else:
            ledger["audit"].append(value)
        if rust_validated(world, KEY):
            accepted += 1
            _validate_state(world)
        else:
            refused += 1
    assert accepted > 0
    assert refused > 300
