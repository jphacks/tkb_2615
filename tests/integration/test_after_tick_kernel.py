"""The end-of-tick check in one Rust call says yes only where the Python check passes.

native/src/rules/after_tick.rs (docs/rust-port/lanes/m6-validate.md). It has no error text: a state
it turns down gets the Python check, with the memo as it was. So what must hold is that a yes means
the Python rules pass, and that the memo it leaves is the one the Python check would leave, because
the scans of a tick and the next check read it.
"""

from __future__ import annotations

import copy
import dataclasses
import random
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core import tick_validation
from sheltersim.core import world as world_module
from sheltersim.core.engine import TickEngine
from sheltersim.core.tick_validation import TickValidationMemo, _is_settled
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain import simulator_state
from sheltersim.domain.infrastructure import INFRASTRUCTURE_EVENT_TYPES
from sheltersim.domain.models import Activity, SupportRequestStatus
from sheltersim.domain.operational import H03_EVENT_TYPES
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

kernel = world_module._after_tick_kernel


@pytest.fixture(scope="module")
def documents(tmp_path_factory, generated_scenario_preview) -> list[dict]:
    """The world every 20 minutes of a Run, as a saved document."""
    scenario = ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    taken: list[dict] = []
    step = TickEngine.step

    def recording(engine: TickEngine) -> Any:
        result = step(engine)
        if engine.world.minute_index % 20 == 0 and engine.world.minute_index > 0:
            taken.append(engine.world.to_dict())
        return result

    patch = pytest.MonkeyPatch()
    patch.setattr(TickEngine, "step", recording)
    try:
        run_scenario(scenario, tmp_path_factory.mktemp("run"))
    finally:
        patch.undo()
    assert taken
    return taken


def _primed(document: dict) -> tuple[WorldState, TickValidationMemo]:
    """A world whose Python check has passed, so the kernel has what it reads."""
    world = WorldState.from_dict(copy.deepcopy(document))
    world.validate_after_tick()
    memo = world.__dict__["_tick_memo"]
    assert memo.steady is not None
    return world, memo


def _kernel(world: WorldState, memo: TickValidationMemo) -> Any:
    with memo.config_scope():
        return kernel(world, memo)


def _takes(world: WorldState, memo: TickValidationMemo) -> bool:
    try:
        _kernel(world, memo)
    except native.Unsupported:
        return False
    return True


def _python_error(world: WorldState) -> str | None:
    try:
        world.validate(validate_event_history=False)
    except StateInvariantError as error:
        return str(error)
    return None


def _memo_facts(memo: TickValidationMemo) -> tuple[Any, ...]:
    return (
        memo.minute,
        tuple(id(item) for item in memo._open_requests),
        tuple(id(item) for item in memo._written_requests),
        len(memo._requests),
        dict(memo._seen),
    )


def test_the_kernel_takes_a_world_the_python_check_passed(documents) -> None:
    for document in documents:
        world, memo = _primed(document)
        assert _kernel(world, memo) == []
        assert _python_error(world) is None


def test_a_tick_past_the_first_goes_through_the_kernel(documents, monkeypatch) -> None:
    world = WorldState.from_dict(copy.deepcopy(documents[0]))
    calls: list[int] = []
    rules = WorldState._validate_with_memo

    def counting(self: WorldState, memo: TickValidationMemo) -> None:
        calls.append(1)
        rules(self, memo)

    monkeypatch.setattr(WorldState, "_validate_with_memo", counting)
    for _ in range(5):
        world.validate_after_tick()
    assert calls == [1]


def _set_item(holder: Any, name: str, value: Any) -> None:
    holder[name] = value


def _remove(world: WorldState, name: str) -> None:
    del world.model_state[name]


def _recent_body_record(world: WorldState) -> dict[str, Any]:
    """A good record of this minute, which the rule reads: what it holds passed the row too, so a
    test of the rule writes past the row with dict.__setitem__, as only the tree holds that."""
    history = world.model_state["body_state_v1"]["history"]
    history.append(
        {
            "event_id": "probe",
            "minute": world.minute_index,
            "kind": "probe",
            "actor_ids": [next(iter(world.agents))],
            "topic_id": None,
            "causation_id": None,
            "deltas": {},
        }
    )
    return history[-1]


def _pending_event(world: WorldState, event_type: str) -> None:
    sequence = world.allocate_event_sequence()
    world.schedule_event(
        ScheduledEvent(
            event_id=f"probe-{sequence}",
            due_minute=world.minute_index + 1,
            priority=0,
            sequence=sequence,
            event_type=event_type,
            kind=EventKind.COMPLETION,
            payload={},
            provenance={},
        )
    )


def _stored_past_the_table(world: WorldState) -> None:
    dict.__setitem__(world.model_state, "movement_v1", dict(world.model_state["movement_v1"]))


BREAKS: dict[str, Callable[[WorldState], Any]] = {
    # ---- What begin compares.
    "a config written": lambda w: _set_item(w.model_configs, "probe", {}),
    "a person's key added past the map": lambda w: dict.__setitem__(w.agents, "new", None),
    "a zone's key removed past the map": lambda w: dict.pop(w.zones, next(iter(w.zones))),
    "a facility moved to another zone": lambda w: setattr(
        next(iter(w.facilities.values())),
        "zone_id",
        next(zone for zone in w.zones if zone != next(iter(w.facilities.values())).zone_id),
    ),
    "a facility moved": lambda w: setattr(
        next(iter(w.facilities.values())), "zone_id", "elsewhere"
    ),
    "the presence state written": lambda w: _set_item(
        w.model_state["population_presence_v1"], "probe", 1
    ),
    "the memo's requests out of the dict": lambda w: dict.pop(
        w.support_requests, next(iter(w.support_requests))
    ),
    # ---- What the table and maps hold.
    "the table replaced": lambda w: w.__dict__.__setitem__("model_state", dict(w.model_state)),
    "a tree stored past the table": _stored_past_the_table,
    # ---- Namespaces a rule requires.
    "the ledger removed": lambda w: _remove(w, "resource_ledger_v1"),
    "the service state removed": lambda w: _remove(w, "resource_service_v1"),
    "the psychology removed": lambda w: _remove(w, "psychological_distress_v1"),
    "the information state removed": lambda w: _remove(w, "information_spread_v1"),
    "the movement state removed": lambda w: _remove(w, "movement_v1"),
    "the body state removed": lambda w: _remove(w, "body_state_v1"),
    "a legacy queue marker": lambda w: _set_item(
        w.model_state, "facility_queues_legacy_mode_v1", "x"
    ),
    # ---- The body state's config and history.
    "the body config": lambda w: _set_item(w.model_state["body_state_v1"]["config"], "probe", 1),
    # ---- The operational events' and the environment's schema.
    "operational events gone": lambda w: _remove(w, "operational_events_v1"),
    "an operational field added": lambda w: _set_item(
        w.model_state["operational_events_v1"], "probe", 1
    ),
    "an operational version": lambda w: _set_item(
        w.model_state["operational_events_v1"], "schema_version", 2
    ),
    "operational shipments": lambda w: _set_item(
        w.model_state["operational_events_v1"], "shipments", []
    ),
    "operational cancellations": lambda w: _set_item(
        w.model_state["operational_events_v1"], "cancelled_staff_task_ids", {}
    ),
    "the environment gone": lambda w: _remove(w, "zone_environment_v1"),
    "an environment field added": lambda w: _set_item(
        w.model_state["zone_environment_v1"], "probe", 1
    ),
    "the environment's modifiers": lambda w: _set_item(
        w.model_state["zone_environment_v1"], "modifiers", []
    ),
    # ---- What a pending event may not be.
    "an infrastructure state without its model": lambda w: _set_item(
        w.model_state, "infrastructure_v1", {}
    ),
    "an infrastructure event without its model": lambda w: _pending_event(w, "power_outage"),
    # ---- A row's validator asked last.
    "a ledger balance below 0": lambda w: _set_item(
        next(iter(w.model_state["resource_ledger_v1"]["accounts"].values())), "remaining", -1
    ),
    # ---- A rule still Python's, called from the kernel.
    "a temporary space state the Python rule turns down": lambda w: _set_item(
        w.model_state["temporary_spaces_v1"], "schema_version", 2
    ),
    # ---- An entity, read from the object by the entity rules.
    "stress above 1 past the row": lambda w: next(iter(w.agents.values())).__dict__.__setitem__(
        "stress", 1.01
    ),
    "a schema version": lambda w: w.__dict__.__setitem__("schema_version", 5),
}


@pytest.mark.parametrize("what", sorted(BREAKS))
def test_a_broken_state_is_left_to_the_python_check_with_the_memo_as_it_was(
    documents, what: str
) -> None:
    world, memo = _primed(documents[0])
    BREAKS[what](world)
    before = _memo_facts(memo)
    assert not _takes(world, memo)
    assert _memo_facts(memo) == before


@pytest.mark.parametrize(
    "what",
    [
        "stress above 1 past the row",
        "the ledger removed",
        "the body config",
        "an operational version",
        "the environment's modifiers",
    ],
)
def test_a_broken_state_stops_the_tick_with_the_python_text(documents, what: str) -> None:
    def error_with(use_kernel: bool) -> str:
        world, memo = _primed(documents[-1])
        if not use_kernel:
            memo.steady = None
        BREAKS[what](world)
        with pytest.raises(StateInvariantError) as raised:
            world.validate_after_tick()
        assert "_tick_memo" not in world.__dict__
        return str(raised.value)

    assert error_with(True) == error_with(False)


def test_every_infrastructure_event_type_needs_the_model(documents) -> None:
    for event_type in sorted(INFRASTRUCTURE_EVENT_TYPES):
        world, memo = _primed(documents[0])
        _pending_event(world, event_type)
        assert not _takes(world, memo), event_type
        assert _python_error(world) == "invalid infrastructure state: " + (
            "infrastructure events require the infrastructure model"
        )


def _without(document: dict, config: str, state: str) -> tuple[WorldState, TickValidationMemo]:
    """A world primed without a model that a pending event would need."""
    document = copy.deepcopy(document)
    document["model_configs"].pop(config)
    document["model_state"].pop(state)
    world = WorldState.from_dict(document)
    world.validate_after_tick()
    memo = world.__dict__["_tick_memo"]
    assert memo.steady is not None
    return world, memo


def test_a_pending_operational_event_needs_its_state(documents) -> None:
    for event_type in sorted(H03_EVENT_TYPES):
        world, memo = _without(documents[0], "operational_events", "operational_events_v1")
        assert _takes(world, memo)
        _pending_event(world, event_type)
        assert not _takes(world, memo), event_type
        assert _python_error(world) is not None


def test_a_pending_environment_event_needs_its_policy(documents, monkeypatch) -> None:
    # A registry that selects an environment formula needs the config, so the plan is made as for
    # a world without either.
    world, memo = _primed(documents[0])
    configs = {k: v for k, v in world.model_configs.items() if k != "zone_environment"}
    monkeypatch.setattr(tick_validation, "field_stamp", lambda *_: (configs, 0))
    memo.steady = tick_validation._steady_plan(SimpleNamespace(model_configs=configs), memo)
    monkeypatch.undo()
    del world.model_state["zone_environment_v1"]
    assert _takes(world, memo)
    _pending_event(world, "effect_program_environment_probe")
    assert not _takes(world, memo)


def test_the_body_history_is_read_from_the_last_record_before_the_tick(documents) -> None:
    world, memo = _primed(documents[-1])
    history = world.model_state["body_state_v1"]["history"]
    if len(history) < 3:
        pytest.skip("this Run has too little history")
    old = history[0]
    assert old["minute"] < world.minute_index - 1 or len(history) < 3
    # Older than the slice: neither rule reads it at a tick.
    if history[1]["minute"] < world.minute_index - 1:
        dict.__setitem__(old, "probe", 1)
        assert _takes(world, memo)
        assert _python_error(world) is None


BODY_RECORD_BREAKS: dict[str, tuple[str, Any]] = {
    "an event ID of another type": ("event_id", 1),
    "a kind of another type": ("kind", None),
    "a minute below 0": ("minute", -1),
    "a minute of another type": ("minute", 1.0),
    "an actor nobody knows": ("actor_ids", ["nobody"]),
    "actors of another type": ("actor_ids", "A001"),
    "deltas of another type": ("deltas", []),
    "a field too many": ("probe", 1),
}


@pytest.mark.parametrize("what", sorted(BODY_RECORD_BREAKS))
def test_a_recent_body_record_past_the_row_is_left_to_the_python_rule(documents, what) -> None:
    world, memo = _primed(documents[0])
    record = _recent_body_record(world)
    world.validate_after_tick()
    assert _takes(world, memo)
    name, value = BODY_RECORD_BREAKS[what]
    dict.__setitem__(record, name, value)
    assert not _takes(world, memo)
    assert _python_error(world) == "invalid simulator extension state"


def test_a_body_record_missing_a_field_past_the_row_is_left_to_the_python_rule(documents) -> None:
    world, memo = _primed(documents[0])
    record = _recent_body_record(world)
    assert _takes(world, memo)
    dict.pop(record, "deltas")
    assert not _takes(world, memo)
    assert _python_error(world) == "invalid simulator extension state"


def test_a_zone_over_its_capacity_is_left_to_the_python_rule(documents) -> None:
    world, memo = _primed(documents[-1])
    zone = world.zones[next(iter(world.agents.values())).zone_id]
    inside = sum(agent.zone_id == zone.id for agent in world.agents.values())
    zone.__dict__["capacity"] = inside - 1
    left = _kernel(world, memo)
    assert left == [(zone.id, inside)]
    with pytest.raises(StateInvariantError, match="exceeds nominal plus approved"):
        world.validate_after_tick()
    assert "_tick_memo" not in world.__dict__


@pytest.mark.parametrize(
    ("holder", "name"),
    [
        (simulator_state, "validate_body_state"),
        (simulator_state, "validate_simulator_extensions"),
        (world_module, "validate_operational_events_state"),
        (world_module, "_entity_rules_kernel"),
        (WorldState, "validate"),
        (TickValidationMemo, "begin"),
    ],
)
def test_a_patch_of_a_rule_leaves_the_tick_to_the_python_check(
    documents, monkeypatch, holder: Any, name: str
) -> None:
    world, memo = _primed(documents[-1])
    original = getattr(holder, name)
    monkeypatch.setattr(holder, name, lambda *a, **k: original(*a, **k))
    assert not _takes(world, memo)
    monkeypatch.undo()
    assert _takes(world, memo)


def test_a_registered_kernel_is_used_and_can_turn_the_state_down(documents, monkeypatch) -> None:
    from sheltersim.core import state_mirror

    world, memo = _primed(documents[-1])
    seen: list[Any] = []

    def no(checked: Any) -> bool:
        seen.append(checked)
        return False

    monkeypatch.setitem(state_mirror._RUST_VALIDATORS, "temporary_spaces_v1", no)
    assert not _takes(world, memo)
    assert seen == [world]
    monkeypatch.setitem(state_mirror._RUST_VALIDATORS, "temporary_spaces_v1", lambda _: True)
    assert _takes(world, memo)
    monkeypatch.delitem(state_mirror._RUST_VALIDATORS, "movement_v1")
    assert not _takes(world, memo)


@pytest.mark.python_rules
def test_the_python_rules_mark_keeps_the_kernel_out(documents) -> None:
    world = WorldState.from_dict(copy.deepcopy(documents[-1]))
    world.validate_after_tick()
    memo = world.__dict__["_tick_memo"]
    assert not _takes(world, memo)


def test_a_configured_infrastructure_keeps_every_tick_python(documents) -> None:
    world = WorldState.from_dict(copy.deepcopy(documents[-1]))
    world.validate_after_tick()
    memo = world.__dict__["_tick_memo"]
    assert memo.steady is not None
    world.model_configs["infrastructure"] = {}
    # The kernel sees the write, and the Python check says what is wrong with the config.
    assert not _takes(world, memo)
    with pytest.raises(StateInvariantError):
        world.validate_after_tick()


def test_a_config_only_a_plan_would_read_does_not_fail_the_check(documents) -> None:
    # Beside its state, the check of a tick doesn't read this config, so it has always passed.
    document = copy.deepcopy(documents[0])
    document["model_configs"]["operational_events"] = {"enabled": "yes"}
    world = WorldState.from_dict(document)
    assert "operational_events_v1" in world.model_state
    world.validate_after_tick()
    world.validate_after_tick()
    assert world.__dict__["_tick_memo"].steady is None


def test_shared_operations_stored_as_none_without_a_contract_are_not_taken(documents) -> None:
    world = WorldState.from_dict(copy.deepcopy(documents[0]))
    world.validate_after_tick()
    memo = world.__dict__["_tick_memo"]
    configs = {k: v for k, v in world.model_configs.items() if k != "shared_operations"}
    patch = pytest.MonkeyPatch()
    patch.setattr(tick_validation, "field_stamp", lambda *_: (configs, 0))
    try:
        memo.steady = tick_validation._steady_plan(SimpleNamespace(model_configs=configs), memo)
    finally:
        patch.undo()
    assert _takes(world, memo) is False  # The state is there, without the contract.
    dict.__setitem__(world.model_state, "shared_operations_v1", None)
    assert not _takes(world, memo)


def _python_memo_after(world: WorldState, memo: TickValidationMemo) -> dict[str, Any]:
    """What the Python check would leave, computed on the side from the memo it would start with."""
    added = memo._requests.added_values(world.support_requests)
    assert added is not None
    checked = {id(item): item for item in memo._open_requests}
    checked.update((id(item), item) for item in memo._written_requests)
    checked.update((id(item), item) for item in added)
    reopened = any(not _is_settled(item) for item in memo._written_requests)
    still_open = [item for item in checked.values() if not _is_settled(item)]
    if reopened:
        still_open = [item for item in world.support_requests.values() if not _is_settled(item)]
    return {
        "open": still_open,
        "settled": [item for item in checked.values() if _is_settled(item)],
        "held": len(memo._requests) + len(added),
    }


def test_the_memo_is_what_the_python_check_leaves_as_requests_change(documents) -> None:
    world, memo = _primed(documents[-1])
    pending = [
        item
        for item in world.support_requests.values()
        if item.status in (SupportRequestStatus.PENDING, SupportRequestStatus.ASSIGNED)
    ]
    settled = [item for item in world.support_requests.values() if _is_settled(item)]

    def step() -> None:
        expected = _python_memo_after(world, memo)
        assert _kernel(world, memo) == []
        assert [id(item) for item in memo._open_requests] == [id(i) for i in expected["open"]]
        assert len(memo._requests) == expected["held"]
        assert memo._written_requests == []
        for item in expected["settled"]:
            assert item._write_listener is memo._written_requests
        for item in expected["open"]:
            assert item._write_listener is None
        assert memo.minute == world.minute_index
        # A scan during the next tick finds every open request, in the dict's order.
        unsettled = world.unsettled_support_requests()
        wanted = [item for item in world.support_requests.values() if not _is_settled(item)]
        assert [item for item in unsettled if not _is_settled(item)] == wanted

    step()
    if settled:
        # A settled request that writes is read again.
        settled[0].cooldown_until = settled[0].cooldown_until
        assert memo._written_requests == [settled[0]]
        step()
    if pending:
        pending[0].last_assignment_wait_reason_code = None
        step()


def test_a_new_request_is_read_and_held(documents) -> None:
    world, memo = _primed(documents[0])
    settled = next(item for item in world.support_requests.values() if _is_settled(item))
    new = dataclasses.replace(settled, id="probe-request")
    world.support_requests[new.id] = new
    held = len(memo._requests)
    assert _kernel(world, memo) == []
    assert len(memo._requests) == held + 1
    assert new._write_listener is memo._written_requests
    # And what's added next is what comes after it.
    assert memo._requests.added_values(world.support_requests) == []
    new.__dict__["requested_minute"] = -1
    new.cooldown_until = new.cooldown_until
    assert memo._written_requests == [new]
    assert not _takes(world, memo)


def test_the_memo_moves_on_to_the_minute_it_checked(documents) -> None:
    world, memo = _primed(documents[0])
    world.__dict__["minute_index"] += 1
    assert _kernel(world, memo) == []
    assert memo.minute == world.minute_index


def _reopened(world: WorldState) -> Any:
    """The first settled request of someone with no other, pending again as the rules want it."""
    open_people = {
        item.agent_id for item in world.support_requests.values() if not _is_settled(item)
    }
    # Before an open one in the dict, so the order of the dict shows.
    last_open = max(
        (
            place
            for place, item in enumerate(world.support_requests.values())
            if not _is_settled(item)
        ),
        default=-1,
    )
    item = next(
        (
            item
            for place, item in enumerate(world.support_requests.values())
            if place < last_open
            and _is_settled(item)
            and item.agent_id not in open_people
            and world.agents[item.agent_id].support_request_id is None
            and world.agents[item.agent_id].activity is Activity.IDLE
        ),
        None,
    )
    if item is None:
        return None
    item.status = SupportRequestStatus.PENDING
    item.resolved_minute = None
    item.assigned_staff_id = None
    item.task_id = None
    world.agents[item.agent_id].support_request_id = item.id
    # A request settled by its worker leaves its timeout in the queue.
    kept = [
        event
        for event in world.event_queue.pending()
        if event.event_type == "support_request_timeout"
        and event.payload.get("request_id") == item.id
    ]
    item.timeout_minute = kept[0].due_minute if kept else world.duration_minutes
    world.agents[item.agent_id].activity = Activity.SEEKING_HELP
    return item


def test_a_reopened_request_comes_back_in_the_order_of_the_dict(documents) -> None:
    for document in documents:
        world, memo = _primed(document)
        item = _reopened(world)
        if item is not None:
            break
    assert item is not None
    assert item in memo._written_requests
    wanted = [req for req in world.support_requests.values() if not _is_settled(req)]
    assert _python_error(world) is None
    assert _kernel(world, memo) == []
    assert memo._open_requests == wanted
    assert item._write_listener is None


def test_random_writes_taken_by_the_kernel_pass_the_python_rules(documents) -> None:
    generator = random.Random(4117)
    values = [None, -1, 0, 1, 2, 1.01, 0.5, True, "", "x", "A001", float("nan"), [], ()]
    taken = refused = 0
    for _ in range(300):
        world, memo = _primed(generator.choice(documents))
        state = world.model_state
        trees = [
            state[name]
            for name in (
                "operational_events_v1",
                "zone_environment_v1",
                "temporary_spaces_v1",
                "shared_operations_v1",
                "population_presence_v1",
                "population_lifecycle_v1",
                "population_dynamics_v1",
                "geometry_overlay_v1",
                "body_state_v1",
            )
        ]
        holders: list[Any] = [
            *trees,
            state["body_state_v1"]["config"],
            *state["body_state_v1"]["history"][-3:],
            *world.agents.values(),
            *world.facilities.values(),
            *world.staff.values(),
        ]
        holder = generator.choice(holders)
        value = generator.choice(values)
        if isinstance(holder, dict):
            name = generator.choice(sorted(holder))
            holder[name] = value
        else:
            name = generator.choice(sorted(holder.__dict__))
            holder.__dict__[name] = value
        if _takes(world, memo):
            taken += 1
            assert _python_error(world) is None, (type(holder).__name__, name, value)
        else:
            refused += 1
    assert taken > 0 and refused > 0
