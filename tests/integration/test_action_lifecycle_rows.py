"""agent_dynamics_v1 owned by Rust, and the open actions and receipts of the action lifecycle.

native/src/rules/{agent_dynamics,action_lifecycle}.rs move the rules; the Python rules stay as the
comparison base and give every error text (docs/rust-port/lanes/f-action-lifecycle.md). The whole
rule is compared on the gate by benchmarks/rule_shadow; here, on small worlds and on values no
scenario reaches.
"""

from __future__ import annotations

import copy
import json
import random
from types import SimpleNamespace
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core import state_mirror
from sheltersim.core.state_mirror import rust_validated
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.action_lifecycle import (
    active_action_index,
    active_action_store,
    finalize_active_actions,
    record_action_proposal,
    record_applied_action_completion,
    record_applied_population_exit,
)
from sheltersim.domain.agent_dynamics import (
    ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION,
    ORIENTATION_FACILITY_SOURCE_ID,
    add_agent,
    agent_knows_facility,
    announce_installed_facility,
    canonical_agent_dynamics_config,
    create_agent_dynamics_state,
    learn_facility,
    record_proposal,
    validate_agent_dynamics_state,
)
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.events.queue import DuplicateEventError, EventLog
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import ActionKind

needs_kernels = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

KEY = "agent_dynamics_v1"


def _text(value: Any) -> str:
    """Float bits, 1 against 1.0, and key order all show."""
    return json.dumps(value)


@pytest.fixture
def world(generated_scenario) -> WorldState:
    scenario = ScenarioSpec.model_validate(
        copy.deepcopy(generated_scenario.model_dump(mode="json"))
    )
    made = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(made)
    return made


def _plain_twin(world: WorldState) -> tuple[Any, SimpleNamespace]:
    plain = json.loads(json.dumps(world.model_state[KEY]))
    table = dict(world.model_state)
    table[KEY] = plain
    return plain, SimpleNamespace(
        model_state=table,
        agents=world.agents,
        facilities=world.facilities,
        model_configs=world.model_configs,
    )


def _proposal(
    agent_id: str, action: ActionKind, minute: int, trigger: str = "t"
) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        action=action,
        minute_index=minute,
        trigger=trigger,
        next_decision_at=minute + 5,
    )


def _agent_id(world: WorldState) -> str:
    return sorted(world.agents)[0]


def _event(
    event_id: str, event_type: str, sequence: int, due: int, **payload: Any
) -> ScheduledEvent:
    return ScheduledEvent(event_id, EventKind.COMPLETION, due, 0, sequence, event_type, payload)


def _applied(world: WorldState, event: ScheduledEvent, minute: int) -> ScheduledEvent:
    world.id_counters["event"] = max(world.id_counters.get("event", 0), event.sequence + 1)
    world.event_log.append(EventLogRecord(event, minute, EventStatus.APPLIED))
    return event


def _lifecycle_documents(world: WorldState) -> list[Any]:
    return [record.to_dict() for record in world.event_log.of_types({"agent_action_lifecycle"})]


class _CollectingLog:
    def __init__(self, log: Any) -> None:
        self.log = log
        self.appended: list[Any] = []

    def find(self, event_id: str) -> Any:
        return self.log.find(event_id)

    def append(self, record: Any) -> None:
        self.appended.append(record)


def _python_twin(world: WorldState) -> Any:
    log = _CollectingLog(world.event_log)
    counter = [world.id_counters.get("event", 0)]

    def allocate() -> int:
        counter[0] += 1
        return counter[0] - 1

    return SimpleNamespace(
        agents=world.agents,
        model_state=world.model_state,
        minute_index=world.minute_index,
        event_log=log,
        allocate_event_sequence=allocate,
        counter=counter,
    )


def _same_as_python(
    world: WorldState, rule: Any, store: Any, *arguments: Any, **options: Any
) -> list[Any]:
    """Run the kernel on the world and the Python rule on a twin: the same receipts and actions."""
    plain = copy.deepcopy(store.snapshot())
    twin = _python_twin(world)
    rule.__wrapped__(twin, *arguments, active_actions=plain, **options)
    before, counter = len(world.event_log), world.id_counters.get("event", 0)
    kernel = getattr(native.rules("action_lifecycle"), rule.__name__)
    kernel(world, *arguments, active_actions=store, **options)
    raws = world.event_log.items_bytes(before, len(world.event_log))
    assert raws == [
        json.dumps(
            record.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        for record in twin.event_log.appended
    ]
    assert world.id_counters.get("event", 0) == twin.counter[0]
    assert counter + len(raws) == twin.counter[0]
    assert _text(store.snapshot()) == _text(plain)
    return twin.event_log.appended


# ---- agent_dynamics_v1: the row ----


@needs_kernels
def test_a_row_writes_the_bytes_of_the_tree_and_a_write_reaches_it(world) -> None:
    mirror = world._core.mirror(KEY)
    assert mirror.row_json() == native.canonical_json(world.model_state[KEY])
    record = world.model_state[KEY]["agents"][_agent_id(world)]
    record["last_action"] = "wait"
    record["known_facility_ids"] = sorted(world.facilities)
    assert mirror.verify()
    assert mirror.row_json() == native.canonical_json(world.model_state[KEY])


@needs_kernels
@pytest.mark.parametrize(
    "key,value",
    [
        ("last_action", 3),
        ("last_target_id", 1.5),
        ("decision_reason", ["a"]),
        ("decision_due_minute", True),
        ("decision_due_minute", 1.0),
        ("decision_due_minute", 10**30),
        ("last_decision_minute", False),
        ("known_facility_ids", ("x",)),
        ("known_facility_ids", [1]),
    ],
)
def test_a_value_the_row_cannot_hold_taints_it_and_the_python_rule_decides(
    world, key: str, value: Any
) -> None:
    agent_id = _agent_id(world)
    world.model_state[KEY]["agents"][agent_id][key] = value
    assert world._core.mirror(KEY).tainted is not None
    assert not rust_validated(world, KEY)
    with pytest.raises(native.Unsupported):
        native.rules("agent_dynamics").record_proposal(
            world, _proposal(agent_id, ActionKind.WAIT, 1), target_id=None
        )


@needs_kernels
def test_the_kernels_write_no_whole_namespace(world) -> None:
    mirror = world._core.mirror(KEY)
    before = mirror.stats()["whole_writes"]
    agent_id = _agent_id(world)
    record_proposal(world, _proposal(agent_id, ActionKind.WAIT, 3), target_id=None)
    learn_facility(world, agent_id, sorted(world.facilities)[0])
    add_agent(world, agent_id)
    assert mirror.stats()["whole_writes"] == before


# ---- agent_dynamics_v1: the rules against the Python rules ----


@needs_kernels
@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"schema_version": 1},
        {"policy_version": "need-led-autonomy-memory-v1"},
        {"policy_version": "need-led-autonomy-memory-v2"},
        {
            "policy_version": ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION,
            "source_id": ORIENTATION_FACILITY_SOURCE_ID,
        },
    ],
)
def test_the_canonical_config_is_the_python_rules(raw: Any) -> None:
    kernel = native.rules("agent_dynamics").canonical_config
    assert _text(kernel(raw)) == _text(canonical_agent_dynamics_config.__wrapped__(raw))


@needs_kernels
@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"other": 1},
        {"schema_version": 2},
        {"schema_version": 1.0},
        {"schema_version": True},
        {"policy_version": "later"},
        {"policy_version": "need-led-autonomy-memory-v1", "source_id": "x"},
        {"policy_version": "need-led-autonomy-memory-v2", "source_id": "x"},
    ],
)
def test_a_config_the_python_rule_refuses_or_coerces_is_left_to_it(raw: Any) -> None:
    with pytest.raises(native.Unsupported):
        native.rules("agent_dynamics").canonical_config(raw)
    python_outcome: Any
    try:
        python_outcome = canonical_agent_dynamics_config.__wrapped__(raw)
    except ValueError as error:
        python_outcome = str(error)
    try:
        assert canonical_agent_dynamics_config(raw) == python_outcome
    except ValueError as error:
        assert str(error) == python_outcome


@needs_kernels
@pytest.mark.parametrize(
    "awareness",
    [
        None,
        {},
        {"discover_co_located_facilities_on_arrival": False},
        {"schema_version": 1, "source_type": "assumption"},
    ],
)
@pytest.mark.parametrize("policy", [None, "need-led-autonomy-memory-v3"])
def test_a_new_namespace_and_agent_are_the_python_rules(
    world, awareness: Any, policy: str | None
) -> None:
    configs = world.model_configs
    configs.pop("agent_dynamics", None)
    configs.pop("facility_awareness", None)
    if awareness is not None:
        configs["facility_awareness"] = {
            **awareness,
            "initial_known_facility_ids": [sorted(world.facilities)[-1]],
        }
    if policy is not None:
        configs["agent_dynamics"] = {"policy_version": policy}
    kernel = native.rules("agent_dynamics").create_state
    assert _text(kernel(world)) == _text(create_agent_dynamics_state.__wrapped__(world))


@needs_kernels
@pytest.mark.parametrize(
    "awareness",
    [
        {"initial_known_facility_ids": ["a", "a"]},
        {"initial_known_facility_ids": ["not stable"]},
        {"initial_known_facility_ids": ("a",)},
        {"discover_co_located_facilities_on_arrival": 1},
        {"source_type": "measured"},
        {"source_id": "other"},
        {"schema_version": 2},
        {"more": 1},
    ],
)
def test_an_awareness_config_the_model_decides_is_left_to_it(world, awareness: Any) -> None:
    world.model_configs["facility_awareness"] = awareness
    with pytest.raises(native.Unsupported):
        native.rules("agent_dynamics").create_state(world)


@needs_kernels
def test_the_writers_are_the_python_rules_on_random_calls(world) -> None:
    rng = random.Random(7)
    agents = sorted(world.agents)
    facilities = sorted(world.facilities)
    rules = native.rules("agent_dynamics")
    world.model_configs["agent_dynamics"] = {
        "policy_version": ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION
    }
    world.model_state[KEY]["agents"][agents[0]]["known_facility_ids"] = []
    state = world.model_state[KEY]
    state_policy = state["policy_version"]
    for step in range(400):
        plain, twin = _plain_twin(world)
        agent_id = rng.choice([*agents, "nobody"])
        facility_id = rng.choice([*facilities, "nowhere"])
        kind = step % 4
        if kind == 0:
            proposal = _proposal(agent_id, rng.choice(list(ActionKind)), step)
            target = rng.choice([None, facility_id])
            if agent_id in plain["agents"]:
                record_proposal.__wrapped__(twin, proposal, target_id=target)
            rules.record_proposal(world, proposal, target_id=target)
        elif kind == 1:
            learn_facility.__wrapped__(twin, agent_id, facility_id)
            rules.learn_facility(world, agent_id, facility_id)
        elif kind == 2:
            if rng.random() < 0.05:
                announce_installed_facility.__wrapped__(twin, facility_id)
                rules.announce_installed_facility(world, facility_id)
        else:
            assert rules.agent_knows_facility(
                world, agent_id, facility_id
            ) == agent_knows_facility.__wrapped__(world, agent_id, facility_id)
        assert _text(plain) == _text(world.model_state[KEY])
        assert world._core.mirror(KEY).verify()
    assert world.model_state[KEY]["policy_version"] == state_policy


@needs_kernels
def test_a_learned_list_that_comes_out_the_same_is_not_written(world) -> None:
    agent_id = _agent_id(world)
    facility_id = sorted(world.facilities)[0]
    learn_facility(world, agent_id, facility_id)
    known = world.model_state[KEY]["agents"][agent_id]["known_facility_ids"]
    learn_facility(world, agent_id, facility_id)
    assert world.model_state[KEY]["agents"][agent_id]["known_facility_ids"] is known


@needs_kernels
def test_a_known_list_out_of_order_is_sorted_as_the_python_rule_does(world) -> None:
    agent_id = _agent_id(world)
    two = sorted(world.facilities)[:2]
    world.model_state[KEY]["agents"][agent_id]["known_facility_ids"] = list(reversed(two))
    plain, twin = _plain_twin(world)
    learn_facility.__wrapped__(twin, agent_id, two[0])
    native.rules("agent_dynamics").learn_facility(world, agent_id, two[0])
    assert _text(plain) == _text(world.model_state[KEY])


@needs_kernels
def test_adding_an_agent_is_the_python_rule(world) -> None:
    agent_id = _agent_id(world)
    world.model_state[KEY]["agents"][agent_id]["last_action"] = "wait"
    plain, twin = _plain_twin(world)
    add_agent.__wrapped__(twin, agent_id)
    native.rules("agent_dynamics").add_agent(world, agent_id)
    assert _text(plain) == _text(world.model_state[KEY])
    native.rules("agent_dynamics").add_agent(world, "nobody")
    assert world._core.mirror(KEY).verify()


def test_without_a_config_every_facility_is_known_to_older_scenarios(world) -> None:
    world.model_configs.pop("agent_dynamics", None)
    world.model_configs.pop("facility_awareness", None)
    assert agent_knows_facility(world, "nobody", "nowhere") is True


# ---- agent_dynamics_v1: what stands behind retiring agent_dynamics_valid ----


MEMORY_SCHEMA = "agent_dynamics_v1 has an invalid exact schema"


def _record_error(agent_id: str) -> str:
    return f"agent_dynamics_v1 record for {agent_id!r} is invalid"


def _minute_error(agent_id: str) -> str:
    return f"agent_dynamics_v1 decision minute for {agent_id!r} is invalid"


def _error(world: WorldState) -> str | None:
    try:
        world.validate(validate_event_history=False)
    except StateInvariantError as error:
        return str(error.__cause__ or error)
    return None


def _python_error(world: WorldState, monkeypatch) -> str | None:
    with monkeypatch.context() as patch:
        patch.setattr(state_mirror, "_RUST_VALIDATORS", {})
        return _error(world)


@pytest.mark.parametrize(
    "write,message",
    [
        ({"known_facility_ids": ["nowhere"]}, _record_error),
        ({"known_facility_ids": "dup"}, _record_error),
        ({"known_facility_ids": ("x",)}, _record_error),
        ({"decision_due_minute": -1}, _record_error),
        ({"decision_due_minute": 1.0}, _record_error),
        ({"last_decision_minute": -1}, _minute_error),
        ({"last_decision_minute": True}, _minute_error),
        ({"last_decision_minute": 1.0}, _minute_error),
    ],
)
def test_a_bad_record_through_a_tracked_dict_stops_with_the_python_text(
    world, monkeypatch, write: dict[str, Any], message: Any
) -> None:
    agent_id = _agent_id(world)
    record = world.model_state[KEY]["agents"][agent_id]
    if write.get("known_facility_ids") == "dup":
        facility = sorted(world.facilities)[0]
        write = {"known_facility_ids": [facility, facility]}
    record.update(write)
    assert not rust_validated(world, KEY)
    found = _error(world)
    assert found is not None and message(agent_id) in found
    assert found == _python_error(world, monkeypatch)


@pytest.mark.parametrize(
    "route",
    ["whole namespace", "whole table", "missing agent", "extra agent", "schema", "policy", "key"],
)
def test_each_way_of_breaking_the_namespace_stops_with_the_python_text(
    world, monkeypatch, route: str
) -> None:
    agent_id = _agent_id(world)
    state = world.model_state[KEY]
    if route == "whole namespace":
        world.model_state[KEY] = {**json.loads(json.dumps(state)), "schema_version": 2}
    elif route == "whole table":
        table = json.loads(json.dumps(state))
        world.model_state = {**world.model_state, KEY: {**table, "policy_version": "other"}}
    elif route == "missing agent":
        del state["agents"][agent_id]
    elif route == "extra agent":
        state["agents"]["nobody"] = json.loads(json.dumps(state["agents"][agent_id]))
    elif route == "schema":
        state["schema_version"] = 2
    elif route == "policy":
        state["policy_version"] = "need-led-autonomy-memory-v2"
    else:
        state["other"] = 1
    assert not rust_validated(world, KEY)
    found = _error(world)
    assert found is not None and MEMORY_SCHEMA in found
    assert found == _python_error(world, monkeypatch)


@needs_kernels
def test_an_agent_who_leaves_the_world_is_seen_by_the_row_kernel(world) -> None:
    assert rust_validated(world, KEY)
    world.agents.pop(_agent_id(world))
    assert not rust_validated(world, KEY)


@needs_kernels
def test_a_good_namespace_is_accepted_and_python_agrees(world) -> None:
    assert rust_validated(world, KEY)
    validate_agent_dynamics_state(world)
    rng = random.Random(3)
    facility = sorted(world.facilities)[0]
    for _ in range(50):
        record = world.model_state[KEY]["agents"][rng.choice(sorted(world.agents))]
        record["decision_due_minute"] = rng.randrange(0, 100)
        record["last_decision_minute"] = rng.choice([None, 0, 5])
        record["known_facility_ids"] = rng.choice([[], [facility]])
        assert rust_validated(world, KEY)
        validate_agent_dynamics_state(world)


# ---- The open actions ----


def _store(world: WorldState) -> Any:
    return active_action_store(world)


@needs_kernels
def test_the_store_holds_rows_and_gives_a_dict_to_a_python_rule(world) -> None:
    store = _store(world)
    assert store.in_rows()
    agent_id = _agent_id(world)
    record_action_proposal(
        world,
        _proposal(agent_id, ActionKind.WAIT, 0),
        target_id=None,
        committed=True,
        active_actions=store,
    )
    assert store.in_rows() and agent_id in store and len(store) == 1
    assert "nobody" not in store and 3 not in store
    expected = store.snapshot()
    plain = store.as_plain()
    assert plain == expected and not store.in_rows()
    plain[agent_id]["continued"] = True
    world.minute_index = 1
    record_action_proposal(
        world,
        _proposal(agent_id, ActionKind.WAIT, 1),
        target_id=None,
        committed=True,
        active_actions=store,
    )
    assert store.in_rows() and store.snapshot()[agent_id]["continued"] is True


@needs_kernels
def test_a_store_that_does_not_fit_stays_with_the_python_rules(world) -> None:
    store = _store(world)
    agent_id = _agent_id(world)
    record_action_proposal(
        world,
        _proposal(agent_id, ActionKind.WAIT, 0),
        target_id=None,
        committed=True,
        active_actions=store,
    )
    store.as_plain()[agent_id]["start_needs"]["fatigue"] = 1
    before = len(world.event_log)
    record_action_proposal(
        world,
        _proposal(agent_id, ActionKind.REST, 1),
        target_id=None,
        committed=True,
        active_actions=store,
    )
    assert not store.in_rows()
    assert len(world.event_log) == before + 2
    assert store.snapshot()[agent_id]["action"] == "rest"


@needs_kernels
def test_a_restored_log_gives_a_store_in_rows(world) -> None:
    store = _store(world)
    agent_id = _agent_id(world)
    record_action_proposal(
        world,
        _proposal(agent_id, ActionKind.WAIT, 0),
        target_id=None,
        committed=True,
        active_actions=store,
    )
    raws = world.event_log.items_bytes(0, len(world.event_log))
    restored = EventLog.from_saved(raws)
    twin = SimpleNamespace(event_log=restored)
    again = active_action_store(twin)
    assert again.in_rows()
    assert again.snapshot()[agent_id]["start_needs"] == dict(
        active_action_index(twin)[agent_id]["start_needs"]
    )
    assert again.snapshot()[agent_id] == store.snapshot()[agent_id]


# ---- The rules against the Python rules, on every path ----


@needs_kernels
def test_a_sequence_of_actions_gives_the_python_rules_receipts(world) -> None:
    rng = random.Random(11)
    store = _store(world)
    agents = sorted(world.agents)[:6]
    actions = [ActionKind.WAIT, ActionKind.REST, ActionKind.SLEEP, ActionKind.FETCH_WATER]
    seen = set()
    for minute in range(300):
        world.minute_index = minute
        agent_id = rng.choice(agents)
        if rng.random() < 0.3:
            world.agents[agent_id].needs.fatigue = rng.random()
        target = rng.choice([None, None, "T1"])
        receipts = _same_as_python(
            world,
            record_action_proposal,
            store,
            _proposal(agent_id, rng.choice(actions), minute),
            target_id=target,
            committed=rng.random() < 0.9,
        )
        seen.update(record.event.payload["transition"] for record in receipts)
    world.minute_index = 300
    receipts = _same_as_python(world, finalize_active_actions, store, 300)
    seen.update(record.event.payload["transition"] for record in receipts)
    assert len(store) == 0
    assert {"started", "continued", "completed", "interrupted", "censored"} <= seen
    for document in _lifecycle_documents(world):
        assert document["status"] == "applied"
        assert (
            document["detail"] == f"autonomous action {document['event']['payload']['transition']}"
        )
        assert document["event"]["event_id"] == f"action-lifecycle-{document['event']['sequence']}"


@needs_kernels
@pytest.mark.parametrize(
    "event_type,action,keys",
    [
        ("toilet_service_complete", ActionKind.REQUEST_TOILET, {"facility_id": "T1"}),
        ("resource_service_complete", ActionKind.FETCH_WATER, {"facility_id": "T1"}),
        ("staff_support_complete", ActionKind.SEEK_HELP, {"request_id": "T1", "staff_id": "S"}),
        ("support_request_timeout", ActionKind.SEEK_HELP, {"request_id": "T1"}),
        ("social_interaction", ActionKind.SOCIALIZE, {"participant_agent_id": "T1"}),
        ("facility_queue_abandon", ActionKind.COLLECT_MEAL, {"facility_id": "T1"}),
        ("resource_service_cancel", ActionKind.COLLECT_MEAL, {"facility_id": "T1"}),
        ("resource_service_cancel", ActionKind.COLLECT_MEAL, {"facility_id": "other"}),
        ("social_interaction", ActionKind.REST, {"participant_agent_id": "T1"}),
        ("agent_travel_complete", ActionKind.REST, {"facility_id": "T1"}),
    ],
)
@pytest.mark.parametrize("sender", [False, True])
def test_an_applied_event_gives_the_python_rules_receipt(
    world, event_type: str, action: ActionKind, keys: dict[str, str], sender: bool
) -> None:
    store = _store(world)
    agent_id = _agent_id(world)
    record_action_proposal(
        world, _proposal(agent_id, action, 2), target_id="T1", committed=True, active_actions=store
    )
    who = {"sender_agent_id": agent_id} if sender else {"agent_id": agent_id}
    event = _applied(world, _event("e-1", event_type, 900, 3, **who, **keys), 3)
    _same_as_python(world, record_applied_action_completion, store, event, 3)


@needs_kernels
def test_events_that_end_nothing_change_nothing(world) -> None:
    store = _store(world)
    agent_id = _agent_id(world)
    record_action_proposal(
        world,
        _proposal(agent_id, ActionKind.FETCH_WATER, 5),
        target_id=None,
        committed=True,
        active_actions=store,
    )
    failed = _event("e-failed", "resource_service_complete", 901, 6, agent_id=agent_id)
    world.event_log.append(EventLogRecord(failed, 6, EventStatus.FAILED))
    assert not _same_as_python(world, record_applied_action_completion, store, failed, 6)
    unknown = _event("e-absent", "resource_service_complete", 902, 6, agent_id=agent_id)
    assert not _same_as_python(world, record_applied_action_completion, store, unknown, 6)
    early = _applied(
        world, _event("e-early", "resource_service_complete", 903, 2, agent_id=agent_id), 6
    )
    assert not _same_as_python(world, record_applied_action_completion, store, early, 6)
    nobody = _applied(world, _event("e-no", "resource_service_complete", 904, 7, agent_id=5), 7)
    assert not _same_as_python(world, record_applied_action_completion, store, nobody, 7)


@needs_kernels
def test_an_agent_gone_from_the_world_closes_without_needs(world) -> None:
    store = _store(world)
    agent_id = _agent_id(world)
    record_action_proposal(
        world,
        _proposal(agent_id, ActionKind.FETCH_WATER, 5),
        target_id=None,
        committed=True,
        active_actions=store,
    )
    event = _applied(world, _event("e-1", "resource_service_cancel", 901, 6, agent_id=agent_id), 6)
    world.agents.pop(agent_id)
    receipts = _same_as_python(world, record_applied_action_completion, store, event, 6)
    assert receipts[0].event.payload["current_needs"] == {}
    assert receipts[0].event.payload["activity"] is None


@needs_kernels
def test_a_population_exit_gives_the_python_rules_receipts(world) -> None:
    store = _store(world)
    agents = sorted(world.agents)
    for agent_id in agents[:4]:
        record_action_proposal(
            world,
            _proposal(agent_id, ActionKind.WAIT, 1),
            target_id=None,
            committed=True,
            active_actions=store,
        )
    event = _applied(world, _event("pop-1", "effect_program_population", 900, 3), 3)
    world.model_state["population_lifecycle_v1"] = {
        "history": [
            {"event_id": "pop-1", "action": "temporary_exit", "agent_id": agents[3]},
            {"event_id": "pop-1", "action": "transferred", "agent_id": agents[1]},
            {"event_id": "pop-1", "action": "returned", "agent_id": agents[0]},
            {"event_id": "other", "action": "transferred", "agent_id": agents[0]},
            "not a dict",
        ]
    }
    world.model_state["population_dynamics_v1"] = {
        "departures": [{"event_id": "pop-1", "agent_id": agents[1]}, {"event_id": "pop-2"}]
    }
    world.agents.pop(agents[1])
    receipts = _same_as_python(world, record_applied_population_exit, store, event, 3)
    assert [record.event.payload["reason_code"] for record in receipts] == [
        "population_departure",
        "population_temporary_exit",
    ]
    other = _event("o-1", "toilet_service_complete", 901, 3, agent_id=agents[0])
    assert not _same_as_python(world, record_applied_population_exit, store, other, 3)


@needs_kernels
@pytest.mark.parametrize(
    "body",
    [{"sleep_debt_minutes": 3, "hygiene_need": 0.25}, {}, {"sleep_debt_minutes": 1.5}],
)
def test_the_body_record_is_read_as_the_python_rule_reads_it(world, body: dict[str, Any]) -> None:
    agent_id = _agent_id(world)
    world.model_state["body_state_v1"]["agents"][agent_id] = body
    store = _store(world)
    receipts = _same_as_python(
        world,
        record_action_proposal,
        store,
        _proposal(agent_id, ActionKind.SLEEP, 0),
        target_id=None,
        committed=True,
    )
    assert receipts[0].event.payload["start_needs"]["sleep_debt_minutes"] == float(
        body.get("sleep_debt_minutes", 0.0)
    )


# ---- What is left to the Python rules, with nothing changed ----


def _refused(world: WorldState, store: Any, proposal: Any, target_id: Any = None) -> None:
    before = len(world.event_log), dict(world.id_counters), _text(store.snapshot())
    with pytest.raises(native.Unsupported):
        native.rules("action_lifecycle").record_action_proposal(
            world, proposal, target_id=target_id, committed=True, active_actions=store
        )
    assert (len(world.event_log), dict(world.id_counters), _text(store.snapshot())) == before


@needs_kernels
@pytest.mark.parametrize("reason", ["", "x" * 121, 5, None])
def test_a_reason_the_receipts_model_decides_is_left_to_it(world, reason: Any) -> None:
    _refused(world, _store(world), _proposal(_agent_id(world), ActionKind.WAIT, 0, reason))


@needs_kernels
@pytest.mark.parametrize("target", ["not stable", 5, "é"])
def test_a_target_the_receipts_model_decides_is_left_to_it(world, target: Any) -> None:
    _refused(world, _store(world), _proposal(_agent_id(world), ActionKind.WAIT, 0), target)


@needs_kernels
@pytest.mark.parametrize("value", [float("nan"), float("inf"), "x", None, True, 2**60])
def test_a_body_value_the_rule_decides_is_left_to_it(world, value: Any) -> None:
    agent_id = _agent_id(world)
    world.model_state["body_state_v1"]["agents"][agent_id] = {"hygiene_need": value}
    _refused(world, _store(world), _proposal(agent_id, ActionKind.WAIT, 0))


@needs_kernels
def test_a_transition_before_its_start_is_left_to_the_receipts_model(world) -> None:
    store = _store(world)
    agent_id = _agent_id(world)
    record_action_proposal(
        world,
        _proposal(agent_id, ActionKind.WAIT, 5),
        target_id=None,
        committed=True,
        active_actions=store,
    )
    _refused(world, store, _proposal(agent_id, ActionKind.WAIT, 4))
    with pytest.raises(ValueError, match="action transition cannot precede its start"):
        record_action_proposal(
            world,
            _proposal(agent_id, ActionKind.WAIT, 4),
            target_id=None,
            committed=True,
            active_actions=store,
        )


@needs_kernels
def test_a_sequence_the_log_holds_is_left_to_the_python_rule_and_its_error(world) -> None:
    store = _store(world)
    agent_id = _agent_id(world)
    next_sequence = world.id_counters.get("event", 0)
    world.event_log.append(
        EventLogRecord(_event("taken", "other_type", next_sequence, 0), 0, EventStatus.APPLIED)
    )
    _refused(world, store, _proposal(agent_id, ActionKind.WAIT, 0))
    with pytest.raises(DuplicateEventError, match="already realized"):
        record_action_proposal(
            world,
            _proposal(agent_id, ActionKind.WAIT, 0),
            target_id=None,
            committed=True,
            active_actions=store,
        )


@needs_kernels
def test_a_tainted_namespace_or_entity_leaves_a_proposal_to_python(world) -> None:
    agent_id = _agent_id(world)
    world.agents[agent_id].needs.fatigue = 1
    store = _store(world)
    with pytest.raises(native.Unsupported):
        native.rules("action_lifecycle").record_action_proposal(
            world,
            _proposal(agent_id, ActionKind.WAIT, 0),
            target_id=None,
            committed=True,
            active_actions=store,
        )


@needs_kernels
def test_a_committed_flag_that_is_not_a_bool_is_left_to_python(world) -> None:
    with pytest.raises(native.Unsupported):
        native.rules("action_lifecycle").record_action_proposal(
            world,
            _proposal(_agent_id(world), ActionKind.WAIT, 0),
            target_id=None,
            committed=1,
            active_actions=_store(world),
        )


@needs_kernels
def test_another_object_of_an_event_is_left_to_the_python_rule(world) -> None:
    store = _store(world)
    agent_id = _agent_id(world)
    record_action_proposal(
        world,
        _proposal(agent_id, ActionKind.FETCH_WATER, 1),
        target_id=None,
        committed=True,
        active_actions=store,
    )
    event = _applied(
        world, _event("e-1", "resource_service_complete", 900, 2, agent_id=agent_id), 2
    )
    copied = _event("e-1", "resource_service_complete", 900, 2, agent_id=agent_id)
    with pytest.raises(native.Unsupported):
        native.rules("action_lifecycle").record_applied_action_completion(
            world, copied, 2, active_actions=store
        )
    record_applied_action_completion(world, copied, 2, active_actions=store)
    assert agent_id not in store
    assert event.event_id == copied.event_id


@needs_kernels
def test_a_counter_a_rule_could_overflow_is_left_to_the_python_rule(world) -> None:
    store = _store(world)
    agent_id = _agent_id(world)
    record_action_proposal(
        world,
        _proposal(agent_id, ActionKind.WAIT, 0),
        target_id=None,
        committed=True,
        active_actions=store,
    )
    world.id_counters["event"] = 2**62 + 1
    _refused(world, store, _proposal(agent_id, ActionKind.REST, 1))
    record_action_proposal(
        world,
        _proposal(agent_id, ActionKind.REST, 1),
        target_id=None,
        committed=True,
        active_actions=store,
    )
    assert world.id_counters["event"] == 2**62 + 3
    assert world.event_log.find(f"action-lifecycle-{2**62 + 2}") is not None


@needs_kernels
def test_a_record_taken_past_the_tree_leaves_the_writers_to_python_with_the_row_as_it_was(
    world,
) -> None:
    agent_id = _agent_id(world)
    facility = sorted(world.facilities)[0]
    mirror = world._core.mirror(KEY)
    agents = world.model_state[KEY]["agents"]
    agents[agent_id]["known_facility_ids"] = []
    dict.pop(agents, agent_id)
    before = mirror.row_json()
    rules = native.rules("agent_dynamics")
    with pytest.raises(native.Unsupported):
        rules.record_proposal(world, _proposal(agent_id, ActionKind.WAIT, 1), target_id=None)
    with pytest.raises(native.Unsupported):
        rules.learn_facility(world, agent_id, facility)
    assert mirror.row_json() == before


# ---- A Run: the kernels really run, and saved identities are kept ----


def test_a_run_saves_the_same_receipts_with_and_without_the_kernels(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    from sheltersim.experiments.runner import run_scenario

    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 90
    scenario = ScenarioSpec.model_validate(document)
    kept = run_scenario(scenario, tmp_path / "kept")
    with monkeypatch.context() as patch:
        from sheltersim.domain import action_lifecycle

        patch.setattr(
            action_lifecycle,
            "active_action_store",
            lambda world: active_action_index(world),
        )
        from sheltersim.experiments import runner

        patch.setattr(runner, "active_action_store", active_action_index)
        python = run_scenario(scenario, tmp_path / "python")
    assert kept.canonical_json() == python.canonical_json()
    assert [record.to_dict() for record in kept.event_log.records] == [
        record.to_dict() for record in python.event_log.records
    ]


@needs_kernels
def test_a_tick_makes_no_lifecycle_object_and_writes_no_whole_namespace(generated_scenario) -> None:
    from sheltersim.core.engine import TickEngine
    from sheltersim.experiments.metrics import MetricsRecorder
    from sheltersim.experiments.timeline import _MetricSink

    scenario = ScenarioSpec.model_validate(generated_scenario.model_dump(mode="json"))
    world = WorldState.from_scenario(scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    whole_writes = None
    for minute in range(30):
        engine.step()
        if minute == 0:
            whole_writes = world._core.mirror(KEY).stats()["whole_writes"]
    assert controller.active_actions.in_rows()
    assert world._core.mirror(KEY).stats()["whole_writes"] == whole_writes
    # Records of the type have no object until something reads them.
    rows = native.state("events").saved_rows(world, "agent_action_lifecycle")
    assert rows
    assert world.event_log.unread() >= len(rows)


def test_two_identical_runs_with_a_restore_between_keep_the_open_actions(
    generated_scenario, tmp_path
) -> None:
    from sheltersim.experiments.runner import run_scenario

    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 45
    world = run_scenario(ScenarioSpec.model_validate(document), tmp_path / "run")
    restored = WorldState.from_dict(world.to_dict())
    store = active_action_store(restored)
    index = active_action_index(world)
    keys = store.snapshot() if native.available() else store
    assert set(keys) == set(index)
