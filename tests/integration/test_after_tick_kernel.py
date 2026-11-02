"""The end-of-tick check in one Rust call says yes only where the Python check passes.

native/src/rules/after_tick.rs (docs/rust-port/lanes/m6-validate.md, l7-validate.md). It has no
error text: a state it turns down gets the Python check, validate(validate_event_history=False),
with no memo. So what must hold is that a yes means the Python rules pass, and that the memo it
leaves is what the next check and the scans of a tick may rely on.

The gate only reaches good states, so each rule the kernel holds is shown here by a state that
breaks it: dropping the rule makes its test fail. The same breaks run on WorldState v5, the world of
a Scenario without resource_service (docs/rust-port/lanes/l7b-world-v5.md), so a rule gated on v6
fails its v5 case.
"""

from __future__ import annotations

import copy
import dataclasses
import random
from collections.abc import Callable
from typing import Any

import pytest
from support.paths import BASELINE_100_7D

from sheltersim import native
from sheltersim.core import tick_validation
from sheltersim.core import world as world_module
from sheltersim.core.context import ContextScope
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain import simulator_state
from sheltersim.domain.environment import ZoneEnvironmentBaseline
from sheltersim.domain.geometry_overlay import _CONFIG_SCOPE
from sheltersim.domain.infrastructure import (
    INFRASTRUCTURE_EVENT_TYPES,
    ScenarioInfrastructureSpec,
    create_infrastructure_runtime_state,
)
from sheltersim.domain.models import Activity, SupportRequestStatus
from sheltersim.domain.operational import H03_EVENT_TYPES
from sheltersim.domain.population_lifecycle import (
    PopulationPresenceCommand,
    apply_population_presence_command,
)
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.benchmark import build_benchmark_scenario
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ResourceServiceConfig, ScenarioSpec

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

kernel = world_module._after_tick_kernel


def _record_run(scenario: ScenarioSpec, directory: Any, keep: Callable[[WorldState], bool]) -> list:
    taken: list[dict] = []
    step = TickEngine.step

    def recording(engine: TickEngine) -> Any:
        result = step(engine)
        if keep(engine.world):
            taken.append(engine.world.to_dict())
        return result

    patch = pytest.MonkeyPatch()
    patch.setattr(TickEngine, "step", recording)
    try:
        run_scenario(scenario, directory)
    finally:
        patch.undo()
    return taken


@pytest.fixture(scope="module")
def runs(tmp_path_factory, generated_scenario_preview) -> dict[str, list[dict]]:
    """Worlds of two Runs, as saved documents: every 20 minutes of the generated Scenario, and, of
    the same with staff who clean, those where a staff member has a task and those where a staff
    operation receipt is among the recent ones."""
    document = copy.deepcopy(generated_scenario_preview["scenario"])
    found: dict[str, list[dict]] = {"every": [], "active": [], "receipt": []}

    def every(world: WorldState) -> bool:
        if world.minute_index % 20 == 0 and world.minute_index > 0:
            found["every"].append(world.to_dict())
        return False

    _record_run(ScenarioSpec.model_validate(document), tmp_path_factory.mktemp("run"), every)
    for person in document["staff"]:
        person["skills"] = sorted({*person["skills"], "cleaning"})
    document["model_configs"]["staff_operations"]["thresholds_by_zone"] = {
        zone["id"]: {"cleaning_sanitation_below": 1.0} for zone in document["zones"]
    }

    def working(world: WorldState) -> bool:
        operations = world.model_state["staff_operations_v1"]
        if len(found["active"]) < 3 and any(
            record["phase"] != "idle" for record in operations["staff"].values()
        ):
            found["active"].append(world.to_dict())
        history = world.model_state["shared_operations_v1"]["history"]
        if len(found["receipt"]) < 3 and any(
            item["event_type"] == "staff_operation_transition"
            and item["minute"] >= world.minute_index - 1
            for item in history[-10:]
        ):
            found["receipt"].append(world.to_dict())
        return False

    _record_run(ScenarioSpec.model_validate(document), tmp_path_factory.mktemp("staff"), working)
    assert all(found.values())
    return found


@pytest.fixture(scope="module")
def documents(runs) -> list[dict]:
    return runs["every"]


def _primed(document: dict) -> tuple[WorldState, Any]:
    """A world whose check the kernel has passed once, so the memo holds what it compares."""
    world = WorldState.from_dict(copy.deepcopy(document))
    taken = native.rules("after_tick").stats()[0]
    world.validate_after_tick()
    assert native.rules("after_tick").stats()[0] == taken + 1
    memo = world.__dict__["_tick_memo"]
    return world, memo


def _kernel(world: WorldState, memo: Any) -> None:
    with ContextScope(_CONFIG_SCOPE, memo.geometry_scope):
        kernel(world, memo)


def _takes(world: WorldState, memo: Any) -> bool:
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


def _is_settled(request: Any) -> bool:
    return request.status in ("resolved", "unresolved")


def _memo_facts(memo: Any) -> tuple[Any, ...]:
    """What a refused check must leave as it was; the configs' plan may be new."""
    facts = memo.facts()
    return (
        facts["minute"],
        tuple(id(item) for item in facts["open"]),
        tuple(id(item) for item in facts["written"]),
        tuple(id(item) for item in facts["requests"]),
        facts["agent_ids"],
        facts["zone_ids"],
        facts["facility_zones"],
        facts["seen"],
    )


def test_the_kernel_takes_a_world_the_python_check_passed(documents) -> None:
    for document in documents:
        world, memo = _primed(document)
        _kernel(world, memo)
        assert _python_error(world) is None


def test_every_tick_goes_through_the_kernel_the_first_too(documents) -> None:
    world = WorldState.from_dict(copy.deepcopy(documents[0]))
    taken = native.rules("after_tick").stats()[0]
    for _ in range(5):
        world.validate_after_tick()
    assert native.rules("after_tick").stats()[0] == taken + 5


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


def _a_person(world: WorldState) -> dict[str, Any]:
    return world.model_state["population_presence_v1"]["people"][next(iter(world.agents))]


def _unknown_baseline(world: WorldState) -> None:
    baselines = world.model_configs["zone_environment"].setdefault("baseline_by_zone", {})
    baselines["nowhere"] = ZoneEnvironmentBaseline().model_dump(mode="json")


BREAKS: dict[str, Callable[[WorldState], Any]] = {
    # ---- The configs, read by the contracts in Python when they change.
    "a resource config field added": lambda w: _set_item(
        w.model_configs["resource_service"], "probe", 1
    ),
    # ---- What the maps of the world hold.
    "a person's key added past the map": lambda w: dict.__setitem__(w.agents, "new", None),
    "a zone's key removed past the map": lambda w: dict.pop(w.zones, next(iter(w.zones))),
    "a facility moved": lambda w: setattr(
        next(iter(w.facilities.values())), "zone_id", "elsewhere"
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
    # ---- The declared presence ledger (rules/tick_validation.rs validate_presence).
    "the presence state written": lambda w: _set_item(
        w.model_state["population_presence_v1"], "probe", 1
    ),
    "a presence archive path to nothing": lambda w: _set_item(
        _a_person(w), "archive_path", ["population_lifecycle_v1", "people", "nobody"]
    ),
    "a presence record from the future": lambda w: _set_item(
        _a_person(w), "since_minute", w.minute_index + 1
    ),
    "a presence record not admitted": lambda w: _set_item(_a_person(w), "status", "departed"),
    "a presence record dropped": lambda w: w.model_state["population_presence_v1"]["people"].pop(
        next(iter(w.agents))
    ),
    "a presence zone unknown": lambda w: _set_item(_a_person(w), "transition_zone_id", "nowhere"),
    # ---- The lifecycle state (rules/population_lifecycle.rs validate).
    "a lifecycle field added": lambda w: _set_item(
        w.model_state["population_lifecycle_v1"], "probe", 1
    ),
    "a lifecycle person who is here": lambda w: _set_item(
        w.model_state["population_lifecycle_v1"]["people"], next(iter(w.agents)), {}
    ),
    # ---- The body state's config and history.
    "the body config": lambda w: _set_item(w.model_state["body_state_v1"]["config"], "probe", 1),
    "the body config names someone unknown": lambda w: _set_item(
        w.model_state["body_state_v1"]["config"],
        "sleep_debt_stress_rate_by_agent",
        {"nobody": 0.001},
    ),
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
    "an environment baseline of an unknown zone": _unknown_baseline,
    # ---- What a pending event may not be.
    "an infrastructure state without its model": lambda w: _set_item(
        w.model_state, "infrastructure_v1", {}
    ),
    "an infrastructure event without its model": lambda w: _pending_event(w, "power_outage"),
    # ---- A row's validator asked last.
    "a ledger balance below 0": lambda w: _set_item(
        next(iter(w.model_state["resource_ledger_v1"]["accounts"].values())), "remaining", -1
    ),
    "a temporary space state turned down": lambda w: _set_item(
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
    assert _python_error(world) is not None or what in {
        # Not broken for the rules: only the kernel can't tell.
        "a schema version",
        "the memo's requests out of the dict",
        "a tree stored past the table",
        "the table replaced",
    }


@pytest.mark.parametrize(
    "what",
    [
        "stress above 1 past the row",
        "the ledger removed",
        "the body config",
        "the body config names someone unknown",
        "an operational version",
        "the environment's modifiers",
        "an environment baseline of an unknown zone",
        "a presence archive path to nothing",
        "a lifecycle field added",
        "a resource config field added",
    ],
)
def test_a_broken_state_stops_the_tick_with_the_python_text(documents, what: str) -> None:
    world, _ = _primed(documents[-1])
    twin = copy.deepcopy(world)
    BREAKS[what](world)
    BREAKS[what](twin)
    expected = _python_error(twin)
    assert expected is not None
    with pytest.raises(StateInvariantError) as raised:
        world.validate_after_tick()
    assert "_tick_memo" not in world.__dict__
    assert str(raised.value) == expected


def test_every_infrastructure_event_type_needs_the_model(documents) -> None:
    for event_type in sorted(INFRASTRUCTURE_EVENT_TYPES):
        world, memo = _primed(documents[0])
        _pending_event(world, event_type)
        assert not _takes(world, memo), event_type
        assert _python_error(world) == "invalid infrastructure state: " + (
            "infrastructure events require the infrastructure model"
        )


def _changed(document: dict, edit: Callable[[dict], None]) -> tuple[WorldState, Any]:
    document = copy.deepcopy(document)
    edit(document)
    return _primed(document)


def _without(config: str, state: str) -> Callable[[dict], None]:
    def edit(document: dict) -> None:
        document["model_configs"].pop(config)
        document["model_state"].pop(state)

    return edit


def test_a_pending_operational_event_needs_its_state(documents) -> None:
    for event_type in sorted(H03_EVENT_TYPES):
        world, memo = _changed(
            documents[0], _without("operational_events", "operational_events_v1")
        )
        _pending_event(world, event_type)
        assert not _takes(world, memo), event_type
        assert _python_error(world) is not None


def test_a_pending_environment_event_needs_its_policy(documents) -> None:
    def edit(document: dict) -> None:
        _without("zone_environment", "zone_environment_v1")(document)
        registry = document["model_configs"].get("policy_registry", {})
        for domain in [key for key in registry if key.startswith("zone_")]:
            registry.pop(domain)

    world, memo = _changed(documents[0], edit)
    _pending_event(world, "effect_program_environment_probe")
    assert not _takes(world, memo)
    assert _python_error(world) is not None


def _with_infrastructure(document: dict) -> None:
    facilities = WorldState.from_dict(copy.deepcopy(document)).facilities
    config = ScenarioInfrastructureSpec().model_dump(mode="json")
    document["model_configs"]["infrastructure"] = config
    document["model_state"]["infrastructure_v1"] = create_infrastructure_runtime_state(
        facilities, [], config
    )


def test_a_world_with_infrastructure_is_the_kernel_s(documents) -> None:
    world, memo = _changed(documents[0], _with_infrastructure)
    base = world.model_state["infrastructure_v1"]["base_working_units"]
    assert base
    _kernel(world, memo)
    facility = next(iter(base))
    for value in (-1, world.facilities[facility].capacity + 1, 1.0, True):
        world, memo = _changed(documents[0], _with_infrastructure)
        world.model_state["infrastructure_v1"]["base_working_units"][facility] = value
        assert not _takes(world, memo), value
        assert _python_error(world) is not None, value
    world, memo = _changed(documents[0], _with_infrastructure)
    world.model_state["infrastructure_v1"]["base_working_units"]["nowhere"] = 0
    assert not _takes(world, memo)
    assert _python_error(world) is not None
    world, memo = _changed(documents[0], _with_infrastructure)
    world.model_state["infrastructure_v1"]["probe"] = {}
    assert not _takes(world, memo)
    assert _python_error(world) is not None


def test_the_body_history_is_read_from_the_last_record_before_the_tick(documents) -> None:
    world, memo = _primed(documents[-1])
    history = world.model_state["body_state_v1"]["history"]
    if len(history) < 3:
        pytest.skip("this Run has too little history")
    old = history[0]
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


def test_a_body_record_of_someone_in_the_population_s_history_is_taken(documents) -> None:
    world, memo = _primed(documents[0])
    population = world.model_state["population_dynamics_v1"]
    population["departures"].append({"agent_id": "gone-before"})
    record = _recent_body_record(world)
    dict.__setitem__(record, "actor_ids", ["gone-before"])
    if _python_error(world) is not None:
        pytest.skip("this Run's population state takes no such departure")
    assert _takes(world, memo)


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
    assert not _takes(world, memo)
    with pytest.raises(StateInvariantError, match="exceeds nominal plus approved"):
        world.validate_after_tick()
    assert "_tick_memo" not in world.__dict__


def test_a_zone_at_its_capacity_is_taken(documents) -> None:
    world, memo = _primed(documents[-1])
    zone = world.zones[next(iter(world.agents.values())).zone_id]
    inside = sum(agent.zone_id == zone.id for agent in world.agents.values())
    zone.__dict__["capacity"] = inside
    assert _python_error(world) is None
    assert _takes(world, memo)


# ---- A staff member with a task: state/staff_operations.rs reads the task, the staff member, the
# graph and the pending events.


def _active(world: WorldState) -> tuple[str, dict[str, Any]]:
    records = world.model_state["staff_operations_v1"]["staff"]
    return next((key, record) for key, record in records.items() if record["phase"] != "idle")


ACTIVE_BREAKS: dict[str, Callable[[WorldState, str, dict[str, Any]], Any]] = {
    "the due minute": lambda w, key, record: _set_item(
        record, "due_minute", record["due_minute"] + 1
    ),
    "the staff member's end, past the row": lambda w, key, record: w.staff[
        key
    ].__dict__.__setitem__("busy_until", record["due_minute"] + 1),
    "the staff member's queue, past the row": lambda w, key, record: w.staff[
        key
    ].__dict__.__setitem__("work_queue", []),
    "the task's worker, past the row": lambda w, key, record: w.active_tasks[
        record["task_id"]
    ].__dict__.__setitem__("staff_id", "somebody"),
    "the target zone": lambda w, key, record: _set_item(record, "target_zone_id", "nowhere"),
    "the target": lambda w, key, record: _set_item(record, "target_id", "nothing"),
    "the travel minutes": lambda w, key, record: _set_item(
        record, "travel_minutes", record["travel_minutes"] + 1
    ),
    "the reason": lambda w, key, record: _set_item(record, "reason", ""),
    "the task kind": lambda w, key, record: _set_item(
        record, "task_kind", "patrol" if record["task_kind"] != "patrol" else "cleaning"
    ),
    "the task's duration in the config": lambda w, key, record: _set_item(
        w.model_state["staff_operations_v1"]["config"]["task_duration_minutes"],
        record["task_kind"],
        w.model_state["staff_operations_v1"]["config"]["task_duration_minutes"][record["task_kind"]]
        + 1,
    ),
}


@pytest.mark.parametrize("what", sorted(ACTIVE_BREAKS))
def test_a_staff_task_that_disagrees_is_left_to_the_python_rule(runs, what: str) -> None:
    if not runs["active"]:
        pytest.skip("this Run gives no staff member a task")
    world, memo = _primed(runs["active"][0])
    key, record = _active(world)
    ACTIVE_BREAKS[what](world, key, record)
    assert not _takes(world, memo)
    assert _python_error(world) is not None


def test_a_staff_task_is_taken(runs) -> None:
    if not runs["active"]:
        pytest.skip("this Run gives no staff member a task")
    for document in runs["active"]:
        world, memo = _primed(document)
        _kernel(world, memo)
        assert _python_error(world) is None


def test_a_staff_task_s_validator_takes_it(runs) -> None:
    if not runs["active"]:
        pytest.skip("this Run gives no staff member a task")
    world, _ = _primed(runs["active"][0])
    assert native.state("staff_operations").validate(world) is True


# ---- A staff operation receipt of this tick: state/shared_operations.rs compares it with the
# retained completions of staff_operations_v1.


def _completion(world: WorldState) -> dict[str, Any]:
    receipt = next(
        item
        for item in reversed(world.model_state["shared_operations_v1"]["history"])
        if item["event_type"] == "staff_operation_transition"
    )
    history = world.model_state["staff_operations_v1"]["history"]
    return next(
        row
        for row in history
        if row["task_id"] == receipt["task_id"] and row["complete_minute"] == receipt["minute"]
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("reason", "another reason"),
        ("start_minute", -1),
        ("staff_id", "somebody"),
        ("causation_event_ids", ["another-cause"]),
    ],
)
def test_a_staff_receipt_without_its_completion_is_left_to_the_python_rule(
    runs, name: str, value: Any
) -> None:
    if not runs["receipt"]:
        pytest.skip("this Run records no staff operation")
    world, memo = _primed(runs["receipt"][0])
    completion = _completion(world)
    if name == "start_minute":
        value = completion["start_minute"] + 1
        if value > completion["complete_minute"]:
            pytest.skip("no room for another start")
    if name == "staff_id":
        value = next(key for key in world.staff if key != completion["staff_id"])
    completion[name] = value
    assert not _takes(world, memo)
    assert _python_error(world) is not None


def test_a_staff_receipt_with_its_completion_is_taken(runs) -> None:
    if not runs["receipt"]:
        pytest.skip("this Run records no staff operation")
    for document in runs["receipt"]:
        world, memo = _primed(document)
        _kernel(world, memo)


# ---- What the kernel holds of Python.


@pytest.mark.parametrize(
    ("holder", "name"),
    [
        (simulator_state, "validate_body_state"),
        (simulator_state, "validate_simulator_extensions"),
        (world_module, "validate_operational_events_state"),
        (world_module, "_entity_rules_kernel"),
        (WorldState, "validate"),
        (tick_validation, "tick_plan"),
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
    assert not _takes(world, world.__dict__["_tick_memo"])


def test_a_config_only_the_python_check_reads_without_a_state_does_not_fail_the_check(
    documents,
) -> None:
    # Beside its state, the check of a tick doesn't read this config, so it has always passed.
    def edit(document: dict) -> None:
        document["model_configs"]["operational_events"] = {"enabled": "yes"}

    world, memo = _changed(documents[0], edit)
    assert "operational_events_v1" in world.model_state
    _kernel(world, memo)
    del world.model_state["operational_events_v1"]
    assert not _takes(world, memo)
    assert _python_error(world) is not None


def test_shared_operations_without_their_contract_are_not_taken(documents) -> None:
    world, memo = _primed(documents[0])
    del world.model_configs["shared_operations"]
    assert not _takes(world, memo)  # The state is there, without the contract.
    assert _python_error(world) is not None
    dict.__setitem__(world.model_state, "shared_operations_v1", None)
    assert not _takes(world, memo)


# ---- The memo.


def _expected_after(world: WorldState, memo: Any) -> dict[str, Any]:
    """What the memo holds after a check of these requests, worked out on the side."""
    facts = memo.facts()
    requests = list(world.support_requests.values())
    held = facts["requests"]
    starts = len(requests) >= len(held) and all(a is b for a, b in zip(held, requests))
    restart = not starts or facts["agent_ids"] != sorted(world.agents)
    if restart:
        checked = requests
        reopened = False
    else:
        checked = list({id(item): item for item in facts["open"] + facts["written"]}.values())
        known = {id(item) for item in checked}
        checked += [item for item in requests[len(held) :] if id(item) not in known]
        reopened = any(not _is_settled(item) for item in facts["written"])
    still_open = [item for item in checked if not _is_settled(item)]
    if reopened:
        still_open = [item for item in requests if not _is_settled(item)]
    return {
        "open": still_open,
        "settled": [item for item in checked if _is_settled(item)],
        "held": len(requests),
    }


def _step(world: WorldState, memo: Any) -> None:
    expected = _expected_after(world, memo)
    _kernel(world, memo)
    facts = memo.facts()
    assert [id(item) for item in facts["open"]] == [id(i) for i in expected["open"]]
    assert len(facts["requests"]) == expected["held"]
    assert facts["written"] == []
    for item in expected["settled"]:
        assert item._write_listener is facts["written"]
    for item in expected["open"]:
        assert item._write_listener is None
    assert facts["minute"] == world.minute_index
    assert facts["agent_ids"] == sorted(world.agents)
    assert facts["zone_ids"] == sorted(world.zones)
    assert facts["facility_zones"] == {k: v.zone_id for k, v in world.facilities.items()}
    # A scan during the next tick finds every open request, in the dict's order.
    unsettled = world.unsettled_support_requests()
    wanted = [item for item in world.support_requests.values() if not _is_settled(item)]
    assert [item for item in unsettled if not _is_settled(item)] == wanted


def test_the_memo_follows_the_requests_as_they_change(documents) -> None:
    world, memo = _primed(documents[-1])
    pending = [
        item
        for item in world.support_requests.values()
        if item.status in (SupportRequestStatus.PENDING, SupportRequestStatus.ASSIGNED)
    ]
    settled = [item for item in world.support_requests.values() if _is_settled(item)]
    _step(world, memo)
    if settled:
        # A settled request that writes is read again.
        settled[0].cooldown_until = settled[0].cooldown_until
        assert memo.facts()["written"] == [settled[0]]
        _step(world, memo)
    if pending:
        pending[0].last_assignment_wait_reason_code = None
        _step(world, memo)


def test_a_new_request_is_read_and_held(documents) -> None:
    world, memo = _primed(documents[0])
    settled = next(item for item in world.support_requests.values() if _is_settled(item))
    new = dataclasses.replace(settled, id="probe-request")
    world.support_requests[new.id] = new
    held = len(memo.facts()["requests"])
    _kernel(world, memo)
    assert len(memo.facts()["requests"]) == held + 1
    assert new._write_listener is memo.facts()["written"]
    new.__dict__["requested_minute"] = -1
    new.cooldown_until = new.cooldown_until
    assert memo.facts()["written"] == [new]
    assert not _takes(world, memo)


def _leaver(world: WorldState) -> str:
    busy = {item.agent_id for item in world.support_requests.values()}
    return next(
        key
        for key, agent in world.agents.items()
        if agent.activity is Activity.IDLE and key not in busy
    )


def _exit(world: WorldState, agent_id: str | None = None) -> str:
    """Someone leaves for a while, as a presence command does it: the people change, and the
    presence ledger points into the lifecycle state."""
    agent_id = agent_id or _leaver(world)
    sequence = world.allocate_event_sequence()
    apply_population_presence_command(
        world,
        ScheduledEvent(
            event_id=f"temporary-exit-{sequence}",
            kind=EventKind.INTERVENTION,
            due_minute=world.minute_index,
            priority=0,
            sequence=sequence,
            event_type="population_presence",
            payload={},
            source_id="SRC-PROBE",
        ),
        PopulationPresenceCommand(
            action="temporary_exit", agent_id=agent_id, source_ids=("SRC-PROBE",)
        ),
    )
    assert agent_id not in world.agents
    return agent_id


def test_a_change_of_people_is_the_kernel_s_and_reads_every_request_again(documents) -> None:
    world, memo = _primed(documents[0])
    _exit(world)
    assert _python_error(world) is None
    _step(world, memo)
    paths = [
        person.get("archive_path")
        for person in world.model_state["population_presence_v1"]["people"].values()
    ]
    assert any(paths)


def test_a_settled_request_is_read_again_when_the_people_change(documents) -> None:
    # A presence command builds the world anew, so the requests are new objects too, and either
    # sends the kernel back to every request (l7-validate.md says which a test can tell apart).
    def broken(world: WorldState) -> None:
        request = next(item for item in world.support_requests.values() if _is_settled(item))
        # Past the report of the write: a settled request depends only on its fields and the
        # people, so the memo skips it until they change; the save's full validation reads it.
        object.__setattr__(request, "requested_minute", world.minute_index + 1)
        assert _python_error(world) is not None

    world, memo = _primed(documents[0])
    broken(world)
    assert _takes(world, memo)
    world, memo = _primed(documents[0])
    _exit(world)
    broken(world)
    assert not _takes(world, memo)


def test_the_body_config_s_past_residents_are_read_at_each_tick(documents) -> None:
    world, memo = _primed(documents[0])
    leaver = _leaver(world)
    body_config = world.model_state["body_state_v1"]["config"]
    body_config["sleep_debt_stress_rate_by_agent"] = {leaver: 0.001}
    _kernel(world, memo)
    # Gone, and still in the population's history: the config may name them.
    _exit(world, leaver)
    _kernel(world, memo)
    # Out of the history too, past the tree's report, so only this rule reads it.
    population = world.model_state["population_dynamics_v1"]
    initial = population["initial_agent_ids"]
    list.__delitem__(initial, initial.index(leaver))
    if leaver in population["profile_traits"]:
        dict.__delitem__(population["profile_traits"], leaver)
    assert _python_error(world) is not None
    assert not _takes(world, memo)


def test_the_memo_moves_on_to_the_minute_it_checked(documents) -> None:
    world, memo = _primed(documents[0])
    world.__dict__["minute_index"] += 1
    _kernel(world, memo)
    assert memo.minute == world.minute_index


def test_a_copied_world_starts_a_memo_of_its_own(documents) -> None:
    world, memo = _primed(documents[0])
    twin = copy.deepcopy(world)
    assert twin.__dict__["_tick_memo"] is not memo
    assert twin.__dict__["_tick_memo"].facts()["minute"] == -1


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
    assert item in memo.facts()["written"]
    wanted = [req for req in world.support_requests.values() if not _is_settled(req)]
    assert _python_error(world) is None
    _kernel(world, memo)
    assert memo.facts()["open"] == wanted
    assert item._write_listener is None


def test_random_writes_taken_by_the_kernel_pass_the_python_rules(runs) -> None:
    generator = random.Random(4117)
    values = [None, -1, 0, 1, 2, 1.01, 0.5, True, "", "x", "A001", float("nan"), [], ()]
    documents = runs["every"] + runs["active"] + runs["receipt"]
    taken = refused = 0
    for _ in range(400):
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
                "staff_operations_v1",
            )
        ]
        holders: list[Any] = [
            *trees,
            state["body_state_v1"]["config"],
            *state["body_state_v1"]["history"][-3:],
            *state["shared_operations_v1"]["history"][-2:],
            *state["staff_operations_v1"]["history"][-2:],
            *state["staff_operations_v1"]["staff"].values(),
            *state["population_presence_v1"]["people"].values(),
            *world.agents.values(),
            *world.facilities.values(),
            *world.staff.values(),
            *world.active_tasks.values(),
        ]
        holder = generator.choice(holders)
        value = generator.choice(values)
        if isinstance(holder, dict):
            if not holder:
                continue
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


# ---- WorldState v5 ------------------------------------------------------------------------------


def _every_20_minutes(scenario: ScenarioSpec, directory: Any) -> list[dict]:
    taken: list[dict] = []

    def every(world: WorldState) -> bool:
        if world.minute_index % 20 == 0 and world.minute_index > 0:
            taken.append(world.to_dict())
        return False

    _record_run(scenario, directory, every)
    assert taken and all(document["schema_version"] == 5 for document in taken)
    return taken


@pytest.fixture(scope="module")
def v5_documents(tmp_path_factory, generated_scenario_preview) -> list[dict]:
    """Worlds of the generated Scenario without resource_service: WorldState v5 with every
    namespace of an operator's Scenario, its ledger and its water and meal points."""
    document = copy.deepcopy(generated_scenario_preview["scenario"])
    del document["model_configs"]["resource_service"]
    return _every_20_minutes(ScenarioSpec.model_validate(document), tmp_path_factory.mktemp("v5"))


@pytest.fixture(scope="module")
def old_form_documents(tmp_path_factory) -> list[dict]:
    """Worlds of the older Scenario form of the fixtures, which is v5 with no model state."""
    scenario = build_benchmark_scenario(
        ScenarioSpec.load(BASELINE_100_7D), agent_count=30, seed=7, duration_minutes=200
    )
    return _every_20_minutes(scenario, tmp_path_factory.mktemp("old-form"))


# Breaks of the resource service, which a v5 world has not.
V6_ONLY_BREAKS = {
    "a resource config field added",
    "the service state removed",
    "the ledger removed",
    "a schema version",
}


def _resource_config() -> dict[str, Any]:
    return ResourceServiceConfig.model_validate(
        {
            "schema_version": 1,
            "policy_version": "resource-service-v1",
            "source_id": "ASM-H02-RESOURCE-SERVICE-V1",
            "source_type": "assumption",
            "service_minutes": 5,
            "water_ml_per_service": 250,
            "food_meal_units_per_service": 1,
            "hydration_growth_per_minute": 0.006,
        }
    ).model_dump(mode="json")


V5_BREAKS: dict[str, Callable[[WorldState], Any]] = {
    # The configs are canonical, so only the version can turn them down.
    "a resource config": lambda w: _set_item(
        w.model_configs, "resource_service", _resource_config()
    ),
    "a resource service state": lambda w: _set_item(
        w.model_state,
        "resource_service_v1",
        {
            "schema_version": 1,
            "cancelled_task_ids": [],
            "active_reservation_ids": [],
            "request_history": [],
            "queue_stall_history": [],
            "stall_state": {},
        },
    ),
    "a hydration need": lambda w: next(iter(w.agents.values())).needs.__dict__.__setitem__(
        "hydration_need", 0.5
    ),
    "a schema version 6": lambda w: w.__dict__.__setitem__("schema_version", 6),
}


def test_the_kernel_takes_the_v5_worlds_of_a_run(v5_documents, old_form_documents) -> None:
    for document in v5_documents + old_form_documents:
        world, memo = _primed(document)
        _kernel(world, memo)
        assert _python_error(world) is None


def test_every_tick_of_a_v5_world_goes_through_the_kernel(v5_documents) -> None:
    world = WorldState.from_dict(copy.deepcopy(v5_documents[0]))
    taken = native.rules("after_tick").stats()[0]
    for _ in range(5):
        world.validate_after_tick()
    assert native.rules("after_tick").stats()[0] == taken + 5


@pytest.mark.parametrize("what", sorted(set(BREAKS) - V6_ONLY_BREAKS) + sorted(V5_BREAKS))
def test_a_broken_v5_state_is_left_to_the_python_check_with_the_memo_as_it_was(
    v5_documents, what: str
) -> None:
    world, memo = _primed(v5_documents[0])
    {**BREAKS, **V5_BREAKS}[what](world)
    before = _memo_facts(memo)
    assert not _takes(world, memo)
    assert _memo_facts(memo) == before
    assert _python_error(world) is not None or what in {
        "the memo's requests out of the dict",
        "a tree stored past the table",
        "the table replaced",
    }


@pytest.mark.parametrize("what", ["a resource config", "a resource service state"])
def test_a_broken_v5_state_stops_the_tick_with_the_python_text(v5_documents, what: str) -> None:
    world, _ = _primed(v5_documents[-1])
    twin = copy.deepcopy(world)
    V5_BREAKS[what](world)
    V5_BREAKS[what](twin)
    expected = _python_error(twin)
    assert expected is not None
    with pytest.raises(StateInvariantError) as raised:
        world.validate_after_tick()
    assert "_tick_memo" not in world.__dict__
    assert str(raised.value) == expected


def test_a_v5_world_needs_no_ledger(v5_documents, old_form_documents) -> None:
    assert all("resource_ledger_v1" not in item["model_state"] for item in old_form_documents)
    world, memo = _primed(v5_documents[0])
    _remove(world, "resource_ledger_v1")
    assert _python_error(world) is None
    _kernel(world, memo)


def test_the_resource_config_of_a_v6_world_is_needed(documents) -> None:
    """Without it the plan has no resource config, and the entity rules turn the world down. In
    a world where no one is being served water or a meal, no other rule reads the config."""
    world, memo = _primed(
        next(
            document
            for document in documents
            if not any(
                task["kind"] in ("resource_service_water", "resource_service_food")
                for task in document["active_tasks"]
            )
        )
    )
    del world.model_configs["resource_service"]
    assert not _takes(world, memo)
    assert _python_error(world) is not None


def test_random_writes_to_a_v5_world_taken_by_the_kernel_pass_the_python_rules(
    v5_documents,
) -> None:
    generator = random.Random(4119)
    values = [None, -1, 0, 1, 2, 1.01, 0.5, 0.0, True, "", "x", "A001", float("nan"), [], ()]
    taken = refused = 0
    for _ in range(300):
        world, memo = _primed(generator.choice(v5_documents))
        state = world.model_state
        holders: list[Any] = [
            state["resource_ledger_v1"],
            *state["resource_ledger_v1"]["accounts"].values(),
            state["body_state_v1"]["config"],
            state["population_presence_v1"],
            *state["population_presence_v1"]["people"].values(),
            state["operational_events_v1"],
            *world.agents.values(),
            *(agent.needs for agent in world.agents.values()),
            *(agent.profile.initial_needs for agent in world.agents.values()),
            *world.facilities.values(),
            *world.staff.values(),
            *world.active_tasks.values(),
        ]
        holder = generator.choice(holders)
        value = generator.choice(values)
        if isinstance(holder, dict):
            if not holder:
                continue
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
