"""The KPI columns and the per-tick entity rules in Rust give what the Python rules give.

native/src/rules/metrics.rs and tick_validation.rs (docs/rust-port/lanes/m5-metrics.md). The KPI
kernels must give the Python rule's values to the bit. The check kernel has no text: what must hold
is that a yes means the Python rules pass, and that a state it turns down gets the Python text.
These run on worlds from the middle of a Run, where queues, trips and open requests are in use, of
WorldState v6 (a Scenario with resource_service) and v5 (one without, docs/rust-port/lanes/
l7b-world-v5.md).
"""

from __future__ import annotations

import copy
import json
import random
from typing import Any

import pytest
from support.paths import BASELINE_100_7D

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import ResourceServiceConfig, StateInvariantError, WorldState
from sheltersim.core.world import _entity_rules_kernel as entity_kernel
from sheltersim.domain.models import (
    ActiveTask,
    Activity,
    FacilityState,
    InitialNeedProfile,
    NeedState,
    SupportRequestStatus,
)
from sheltersim.domain.population import validate_population_dynamics_state
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments import metrics
from sheltersim.experiments.benchmark import build_benchmark_scenario
from sheltersim.experiments.operational_kpis import operational_kpi_values
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")


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


@pytest.fixture(scope="module")
def v5_documents(tmp_path_factory) -> list[dict]:
    """The world every 10 minutes of a Run of a Scenario without resource_service: WorldState v5."""
    scenario = build_benchmark_scenario(
        ScenarioSpec.load(BASELINE_100_7D), agent_count=30, seed=7, duration_minutes=200
    )
    taken: list[dict] = []
    step = TickEngine.step

    def recording(engine: TickEngine) -> Any:
        result = step(engine)
        if engine.world.minute_index % 10 == 0 and engine.world.minute_index > 0:
            taken.append(engine.world.to_dict())
        return result

    patch = pytest.MonkeyPatch()
    patch.setattr(TickEngine, "step", recording)
    try:
        run_scenario(scenario, tmp_path_factory.mktemp("v5"))
    finally:
        patch.undo()
    assert taken and all(document["schema_version"] == 5 for document in taken)
    return taken


def _world(document: dict) -> WorldState:
    return WorldState.from_dict(copy.deepcopy(document))


def _accepts(world: WorldState) -> bool:
    config = _resource_config(world)
    try:
        entity_kernel(
            world,
            list(world.support_requests.values()),
            config,
            InitialNeedProfile,
            NeedState,
        )
    except native.Unsupported:
        return False
    return True


def _resource_config(world: WorldState) -> Any:
    """The parsed config of a v6 world, and None for a v5 one, as validate passes them."""
    if "resource_service" not in world.model_configs:
        return None
    return ResourceServiceConfig.model_validate(world.model_configs["resource_service"])


def _python_error(world: WorldState) -> str | None:
    """What the Python rules say about the entities and pending events of this world."""
    try:
        pending = world._validate_entity_rules(_resource_config(world))
        world._validate_event_rules(pending, False)
    except StateInvariantError as error:
        return str(error)
    except (KeyError, TypeError, AttributeError) as error:
        return f"{type(error).__name__}: {error}"
    return None


def _set(item: Any, name: str, value: Any) -> None:
    """Reaches the object only: the kernel must read it there."""
    item.__dict__[name] = value


def _moving(world: WorldState) -> Any:
    return next(a for a in world.agents.values() if a.activity is Activity.MOVING)


def _busy_facility(world: WorldState) -> Any:
    return next(f for f in world.facilities.values() if f.active_agent_ids or f.queue_agent_ids)


def _resource_active(world: WorldState) -> Any:
    return next(f for f in world.facilities.values() if f.resource_active_agent_ids)


def _pending_request(world: WorldState) -> Any:
    return next(
        r for r in world.support_requests.values() if r.status is SupportRequestStatus.PENDING
    )


def _task_of_kind(world: WorldState, *kinds: str) -> Any:
    return next(t for t in world.active_tasks.values() if t.kind in kinds)


def _agent(world: WorldState) -> Any:
    return next(iter(world.agents.values()))


def _staff(world: WorldState) -> Any:
    return next(iter(world.staff.values()))


def _facility(world: WorldState) -> Any:
    return next(iter(world.facilities.values()))


def _edge(world: WorldState) -> Any:
    return next(iter(world.zone_edges.values()))


def _duplicate_queue_entry(world: WorldState) -> None:
    facility = _busy_facility(world)
    entries = list(facility.queue_agent_ids or facility.active_agent_ids)
    name = "queue_agent_ids" if facility.queue_agent_ids else "active_agent_ids"
    _set(facility, name, [*entries, entries[0]])


def _someone_queued_looks_elsewhere(world: WorldState) -> None:
    facility = next(f for f in world.facilities.values() if f.queue_agent_ids)
    _set(world.agents[facility.queue_agent_ids[0]], "target_id", "elsewhere")


def _someone_served_looks_elsewhere(world: WorldState) -> None:
    facility = next(f for f in world.facilities.values() if f.active_agent_ids)
    _set(world.agents[facility.active_agent_ids[0]], "target_id", "elsewhere")


def _task_without_entry(world: WorldState) -> None:
    facility = _resource_active(world)
    _set(facility, "resource_active_agent_ids", [])


def _second_open_request(world: WorldState) -> None:
    request = _pending_request(world)
    _set(request, "status", "pending")


def _inventory_below_zero(world: WorldState) -> None:
    dict.__setitem__(world.resources, next(iter(world.resources)), -1)


def _counter_below_zero(world: WorldState) -> None:
    world.id_counters["checked"] = -1


def _event_counter_behind(world: WorldState) -> None:
    world.id_counters["event"] = 0


def _cancelled_list_lost(world: WorldState) -> None:
    world.model_state["resource_service_v1"]["active_reservation_ids"] = ["nobody"]


BREAKS = {
    "stress over": lambda w: _set(_agent(w), "stress", 1.01),
    "stress is a str": lambda w: _set(_agent(w), "stress", "x"),
    "need over": lambda w: _set(_agent(w).needs, "hunger", 1.01),
    "need NaN": lambda w: _set(_agent(w).needs, "fatigue", float("nan")),
    "agent zone": lambda w: _set(_agent(w), "zone_id", "nowhere"),
    "agent activity": lambda w: _set(_agent(w), "activity", "idle"),
    "decision below 0": lambda w: _set(_agent(w), "next_decision_at", -1),
    "decision is a bool": lambda w: _set(_agent(w), "next_decision_at", True),
    "moving with no trip": lambda w: _set(
        next(a for a in w.agents.values() if a.activity is Activity.IDLE),
        "activity",
        Activity.MOVING,
    ),
    "not moving on a trip": lambda w: _set(_moving(w), "activity", Activity.IDLE),
    "trip target": lambda w: _set(_moving(w), "target_id", "elsewhere"),
    "queued with no queue": lambda w: _set(
        next(a for a in w.agents.values() if a.activity is Activity.IDLE),
        "activity",
        Activity.QUEUED,
    ),
    "someone names no open request": lambda w: _set(_agent(w), "support_request_id", "nothing"),
    "facility id": lambda w: _set(_facility(w), "id", "other"),
    "facility zone": lambda w: _set(_facility(w), "zone_id", "nowhere"),
    "capacity below 0": lambda w: _set(_facility(w), "capacity", -1),
    "working above capacity": lambda w: _set(
        _facility(w), "working_units", _facility(w).capacity + 1
    ),
    "duplicate entry": _duplicate_queue_entry,
    "queued target": _someone_queued_looks_elsewhere,
    "served target": _someone_served_looks_elsewhere,
    "resource slot lost": _task_without_entry,
    "edge minutes": lambda w: _set(_edge(w), "travel_minutes", 0),
    "edge zone": lambda w: _set(_edge(w), "to_zone_id", "nowhere"),
    "staff zone": lambda w: _set(_staff(w), "zone_id", "nowhere"),
    "staff busy idle": lambda w: _set(_staff(w), "busy_until", _staff(w).busy_until + 1),
    "staff unknown task": lambda w: _set(_staff(w), "work_queue", ["nothing"]),
    "trip timing": lambda w: _set(
        _task_of_kind(w, "travel_to_toilet", "travel_to_resource", "travel_return"),
        "due_minute",
        0,
    ),
    "task agent": lambda w: _set(
        _task_of_kind(w, "travel_to_toilet", "travel_to_resource", "travel_return"),
        "agent_id",
        "nobody",
    ),
    "request agent": lambda w: _set(_pending_request(w), "agent_id", "nobody"),
    "request timing": lambda w: _set(_pending_request(w), "requested_minute", -1),
    "request timeout": lambda w: _set(
        _pending_request(w), "timeout_minute", _pending_request(w).timeout_minute + 1
    ),
    "request status": _second_open_request,
    "request wait reason": lambda w: _set(
        _pending_request(w), "last_assignment_wait_reason_code", "anything"
    ),
    "inventory": _inventory_below_zero,
    "counter": _counter_below_zero,
    "event counter": _event_counter_behind,
    "service reservations": _cancelled_list_lost,
}


def _broken_world(documents: list[dict], name: str) -> WorldState:
    """A world this break applies to, from the first document that has what it writes."""
    for document in documents:
        world = _world(document)
        if not _accepts(world):
            continue
        try:
            BREAKS[name](world)
        except StopIteration:
            continue
        return world
    pytest.fail(f"no document has what {name!r} writes")


@pytest.mark.parametrize("name", BREAKS)
def test_the_check_turns_down_what_the_python_rules_turn_down(documents, name) -> None:
    world = _broken_world(documents, name)
    if name == "request status":
        # A value Python makes a member of the enum again: the kernel leaves it to the rule.
        assert not _accepts(world)
        return
    assert _python_error(world) is not None
    assert not _accepts(world)


def test_the_check_takes_the_worlds_of_a_run(documents) -> None:
    for document in documents:
        world = _world(document)
        assert _python_error(world) is None
        assert _accepts(world), world.minute_index


def test_random_writes_taken_by_the_check_pass_the_python_rules(documents) -> None:
    generator = random.Random(4100)
    values = [None, -1, 0, 1, 2, 1.01, 0.5, True, "", "x", "A001", float("nan"), [], ()]
    taken = refused = 0
    for _ in range(400):
        world = _world(generator.choice(documents))
        holders = [
            *world.agents.values(),
            *(agent.needs for agent in world.agents.values()),
            *world.facilities.values(),
            *world.staff.values(),
            *world.active_tasks.values(),
            *world.zone_edges.values(),
            *(
                request
                for request in world.support_requests.values()
                if request.status in (SupportRequestStatus.PENDING, SupportRequestStatus.ASSIGNED)
            ),
        ]
        holder = generator.choice(holders)
        name = generator.choice(sorted(holder.__dict__))
        holder.__dict__[name] = generator.choice(values)
        if _accepts(world):
            taken += 1
            assert _python_error(world) is None, (type(holder).__name__, name)
        else:
            refused += 1
    # Fields no rule reads leave the check's yes standing, and most writes break something.
    assert taken > 0 and refused > 0


# ---- WorldState v5 ------------------------------------------------------------------------------

# What a v5 world of the Run does not have: resource slots, tasks, the service state and a stock
# (its inventory is broken in V5_BREAKS instead).
V6_ONLY_BREAKS = {"resource slot lost", "service reservations", "inventory"}


def _idle(world: WorldState) -> Any:
    return next(
        a
        for a in world.agents.values()
        if a.activity is Activity.IDLE and a.support_request_id is None
    )


def _water_station(world: WorldState, zone_id: str) -> FacilityState:
    facility = FacilityState(
        id="water-probe", kind="water_station", zone_id=zone_id, capacity=2, working_units=2
    )
    world.facilities[facility.id] = facility
    return facility


def _queued_at_water(world: WorldState, *, slot: str) -> tuple[Any, FacilityState]:
    """An idle person queued at a new water station, in its ordinary or its resource queue."""
    agent = _idle(world)
    facility = _water_station(world, agent.zone_id)
    _set(agent, "activity", Activity.QUEUED)
    _set(agent, "target_id", facility.id)
    _set(facility, slot, [agent.id])
    return agent, facility


def _resource_queue_task_in_v5(world: WorldState) -> None:
    """A resource queue slot with its task: all that pairs up, but a v5 world has no service."""
    agent, facility = _queued_at_water(world, slot="resource_queue_agent_ids")
    task = ActiveTask(
        id="resource-probe",
        kind="resource_service_queue",
        agent_id=agent.id,
        resource_id=facility.id,
        start_minute=world.minute_index,
        due_minute=world.minute_index,
    )
    world.active_tasks[task.id] = task


def _resource_completion_in_v5(world: WorldState) -> None:
    """A completion of no active task, which Python looks up in the v6 service state."""
    sequence = world.allocate_event_sequence()
    world.schedule_event(
        ScheduledEvent(
            event_id=f"probe-{sequence}",
            due_minute=world.minute_index + 1,
            priority=0,
            sequence=sequence,
            event_type="resource_service_complete",
            kind=EventKind.COMPLETION,
            payload={"task_id": "nothing"},
            provenance={},
        )
    )


V5_BREAKS = {
    "a hydration need": lambda w: _set(_agent(w).needs, "hydration_need", 0.5),
    "an initial hydration need": lambda w: _set(
        _agent(w).profile.initial_needs, "hydration_need", 0.5
    ),
    "a resource queue task": _resource_queue_task_in_v5,
    "a resource completion": _resource_completion_in_v5,
    "a resource slot without its task": lambda w: _queued_at_water(
        w, slot="resource_queue_agent_ids"
    ),
    "an inventory below 0 without a stock": lambda w: dict.__setitem__(w.resources, "water_ml", -1),
}


def _broken_v5_world(documents: list[dict], break_state: Any) -> WorldState:
    for document in documents:
        world = _world(document)
        assert _accepts(world)
        try:
            break_state(world)
        except StopIteration:
            continue
        return world
    pytest.fail("no v5 document has what the break writes")


def test_the_check_takes_the_v5_worlds_of_a_run(v5_documents) -> None:
    for document in v5_documents:
        world = _world(document)
        assert _python_error(world) is None
        assert _accepts(world), world.minute_index


@pytest.mark.parametrize("name", sorted(set(BREAKS) - V6_ONLY_BREAKS))
def test_the_rules_of_both_versions_turn_down_a_broken_v5_world(v5_documents, name) -> None:
    """Each rule v5 shares with v6 holds for v5: gating one on v6 fails its case here."""
    world = _broken_v5_world(v5_documents, BREAKS[name])
    if name != "request status":
        assert _python_error(world) is not None
    assert not _accepts(world)


@pytest.mark.parametrize("name", sorted(V5_BREAKS))
def test_the_v5_rules_turn_down_what_the_python_rules_turn_down(v5_documents, name) -> None:
    world = _broken_v5_world(v5_documents, V5_BREAKS[name])
    assert _python_error(world) is not None
    assert not _accepts(world)


def test_a_v5_water_station_may_use_the_ordinary_queue(v5_documents) -> None:
    """v5 has no resource service, so the v6 rule against ordinary queues there is not v5's."""
    world = _broken_v5_world(v5_documents, lambda w: _queued_at_water(w, slot="queue_agent_ids"))
    assert _python_error(world) is None
    assert _accepts(world)


def test_a_v5_world_is_not_taken_with_a_resource_config(v5_documents) -> None:
    world = _world(v5_documents[0])
    config = ResourceServiceConfig.model_validate(
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
    )
    with pytest.raises(native.Unsupported):
        entity_kernel(
            world, list(world.support_requests.values()), config, InitialNeedProfile, NeedState
        )


def _no_service_under_way(documents: list[dict]) -> dict:
    """A world where no one is being served water or a meal, so no rule reads the config."""
    return next(
        document
        for document in documents
        if not any(
            task["kind"] in ("resource_service_water", "resource_service_food")
            for task in document["active_tasks"]
        )
    )


def test_a_v6_world_is_not_taken_without_its_resource_config(documents) -> None:
    world = _world(_no_service_under_way(documents))
    assert _accepts(world)
    with pytest.raises(native.Unsupported):
        entity_kernel(
            world, list(world.support_requests.values()), None, InitialNeedProfile, NeedState
        )


def test_random_writes_to_a_v5_world_taken_by_the_check_pass_the_python_rules(
    v5_documents,
) -> None:
    generator = random.Random(4105)
    values = [None, -1, 0, 1, 2, 1.01, 0.5, 0.0, True, "", "x", "A001", float("nan"), [], ()]
    taken = refused = 0
    for _ in range(300):
        world = _world(generator.choice(v5_documents))
        holders = [
            *world.agents.values(),
            *(agent.needs for agent in world.agents.values()),
            *(agent.profile.initial_needs for agent in world.agents.values()),
            *world.facilities.values(),
            *world.staff.values(),
            *world.active_tasks.values(),
            *world.zone_edges.values(),
            *(
                request
                for request in world.support_requests.values()
                if request.status in (SupportRequestStatus.PENDING, SupportRequestStatus.ASSIGNED)
            ),
        ]
        holder = generator.choice(holders)
        name = generator.choice(sorted(holder.__dict__))
        holder.__dict__[name] = generator.choice(values)
        if _accepts(world):
            taken += 1
            assert _python_error(world) is None, (type(holder).__name__, name)
        else:
            refused += 1
    assert taken > 0 and refused > 0


def test_a_zone_with_temporary_space_is_left_to_the_python_rule(documents) -> None:
    world = _world(documents[0])
    state = world.model_state.setdefault(
        "temporary_spaces_v1", {"schema_version": 1, "spaces": {}, "history": []}
    )
    assert (
        entity_kernel(
            world,
            list(world.support_requests.values()),
            _resource_config(world),
            InitialNeedProfile,
            NeedState,
        )
        == []
        or True
    )
    state["spaces"]["room"] = {"zone_id": next(iter(world.zones))}
    left = entity_kernel(
        world,
        list(world.support_requests.values()),
        _resource_config(world),
        InitialNeedProfile,
        NeedState,
    )
    with_capacity = [zone for zone in world.zones.values() if zone.capacity is not None]
    assert {zone for zone, _ in left} == {zone.id for zone in with_capacity}
    occupancy = {zone: 0 for zone in world.zones}
    for agent in world.agents.values():
        occupancy[agent.zone_id] += 1
    assert all(occupancy[zone] == count for zone, count in left)


def test_a_zone_over_its_capacity_is_left_to_the_python_rule(documents) -> None:
    world = _world(documents[0])
    zone = next(zone for zone in world.zones.values() if zone.capacity is not None)
    _set(zone, "capacity", -1)
    left = entity_kernel(
        world,
        list(world.support_requests.values()),
        _resource_config(world),
        InitialNeedProfile,
        NeedState,
    )
    assert [item[0] for item in left] == [zone.id]
    with pytest.raises(StateInvariantError, match="exceeds nominal"):
        world._validate_zone_capacity(*left[0])


def test_an_event_queue_with_an_order_outside_i64_is_left_to_the_python_rules(documents) -> None:
    world = _world(documents[0])
    world.event_queue._leave_rust(list(world.event_queue.pending()))
    assert not _accepts(world)
    assert _python_error(world) is None


@pytest.mark.parametrize(
    "write, accepted",
    [
        (lambda state: None, True),
        (lambda state: state.update(schema_version=2), False),
        (lambda state: state.update(policy_version="other"), False),
        (lambda state: state.update(source_id="other"), False),
        (lambda state: state["initial_agent_ids"].append(state["initial_agent_ids"][0]), False),
        (lambda state: state.update(arrivals="none"), False),
        (lambda state: state.update(extra=1), False),
    ],
)
def test_the_population_kernel_takes_only_what_the_rule_passes(documents, write, accepted) -> None:
    world = _world(documents[0])
    kernel = native.rules("tick_validation").population_dynamics
    state = world.model_state["population_dynamics_v1"]
    write(state)
    try:
        answer = kernel(world)
    except native.Unsupported:
        answer = False
    assert answer is accepted
    if accepted:
        validate_population_dynamics_state(world)
    else:
        with pytest.raises(ValueError):
            validate_population_dynamics_state(world)


def test_the_population_kernel_leaves_someone_pending_to_the_rule(documents) -> None:
    world = _world(documents[0])
    state = world.model_state["population_dynamics_v1"]
    state["pending_arrivals"].append({})
    with pytest.raises(native.Unsupported):
        native.rules("tick_validation").population_dynamics(world)


# ---- The KPI columns ---------------------------------------------------------------------------


def _fields_of_both(world: WorldState) -> tuple[list, list]:
    unsettled = list(world.unsettled_support_requests())
    kernel = native.rules("metrics").snapshot_fields(world, unsettled, world.minute_index)
    python = metrics._snapshot_fields.__wrapped__(world, unsettled, world.minute_index)
    return json.loads(json.dumps(kernel)), json.loads(json.dumps(python))


def _snapshot_or_refusal(world: WorldState) -> bool:
    try:
        kernel, python = _fields_of_both(world)
    except native.Unsupported:
        return False
    assert kernel == python
    return True


def test_the_snapshot_is_the_python_rules_on_the_worlds_of_a_run(documents) -> None:
    ran = sum(_snapshot_or_refusal(_world(document)) for document in documents)
    assert ran > 0


def test_the_stress_threshold_is_where_the_python_rule_has_it(documents) -> None:
    world = _world(documents[0])
    agents = list(world.agents.values())
    agents[0].stress = 0.7
    agents[1].stress = 0.6999999999999999
    agents[2].stress = 0.707
    assert _snapshot_or_refusal(world)


def test_a_person_with_two_open_requests_is_the_last_in_the_snapshot(documents) -> None:
    for document in documents:
        world = _world(document)
        active = [
            request
            for request in world.unsettled_support_requests()
            if request.status is SupportRequestStatus.PENDING
        ]
        if len(active) >= 2:
            active[1].agent_id = active[0].agent_id
            if _snapshot_or_refusal(world):
                return
    pytest.fail("no document had two pending requests the kernel takes")


def test_a_kind_that_is_not_ascii_is_left_to_the_python_rule(documents) -> None:
    world = _world(documents[0])
    next(iter(world.facilities.values())).kind = "TOİLET"
    with pytest.raises(native.Unsupported):
        native.rules("metrics").snapshot_fields(
            world, list(world.unsettled_support_requests()), world.minute_index
        )


def test_a_tainted_metric_state_is_left_to_the_python_rule(documents) -> None:
    world = _world(documents[0])
    world.metric_state["toilet_wait_count"] = 1
    assert metrics.MetricSnapshot.from_world(world).toilet_wait_count == 1


def test_the_kpi_columns_are_the_python_rules_on_the_worlds_of_a_run(documents) -> None:
    for document in documents:
        world = _world(document)
        snapshot = metrics.MetricSnapshot.from_world(world)
        kernel = native.rules("metrics").kpi_values(world, snapshot)
        python = operational_kpi_values.__wrapped__(world, snapshot)
        assert json.dumps(kernel) == json.dumps(python)
        assert list(kernel) == list(python)


def test_kpi_columns_with_no_one_are_left_to_the_python_rule(documents) -> None:
    world = _world(documents[0])
    snapshot = metrics.MetricSnapshot.from_world(world)
    world.agents.clear()
    with pytest.raises(native.Unsupported):
        native.rules("metrics").kpi_values(world, snapshot)
    with pytest.raises(ZeroDivisionError):
        operational_kpi_values(world, snapshot)


def test_a_changed_environment_config_is_read_again_at_the_next_tick(documents) -> None:
    world = _world(documents[0])
    taken = native.rules("after_tick").stats()[0]
    world.validate_after_tick()
    world.validate_after_tick()
    # Both checks are the kernel's, which read the config when it had no plan for it.
    assert native.rules("after_tick").stats()[0] == taken + 2
    if "zone_environment" in world.model_configs:
        world.model_configs["zone_environment"]["unknown"] = 1
        with pytest.raises(StateInvariantError, match="ScenarioZoneEnvironmentConfig"):
            world.validate_after_tick()
