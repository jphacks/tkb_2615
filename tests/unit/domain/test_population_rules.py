"""The kernels of population.py and population_lifecycle.py against their Python rules.

The gate reaches these rules at a few events, so what it can't reach is compared here: each case
runs the Python rule on a copy of the world and the kernel on the world, and the two must leave
the same document and return the same value. A kernel refuses what the rule raises on, before it
changes anything, and the rule then raises its own text.
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest

from sheltersim import native
from sheltersim.contracts.presence import PRESENCE_NAMESPACE, PresenceState
from sheltersim.core.world import WorldState
from sheltersim.domain import population, population_lifecycle
from sheltersim.domain.population_lifecycle import (
    POPULATION_LIFECYCLE_STATE_KEY,
    PopulationPresenceCommand,
    initialize_population_lifecycle_state,
)
from sheltersim.events.effects import (
    EffectProgram,
    EffectProgramGeometryHandler,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
)
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventApplicationError, EventKind, ScheduledEvent
from sheltersim.experiments.scenario import ScenarioSpec

pytestmark = pytest.mark.skipif(not native.available(), reason="the kernels are the subject")


def _document(world: WorldState) -> str:
    # A digest: pytest's report of two long documents that differ takes minutes.
    text = world._canonical_text(world._canonical_data())
    return hashlib.sha256(text.encode()).hexdigest()


def _event(event_id: str = "arrival-event", minute: int = 0, **payload: Any) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=event_id,
        kind=EventKind.INTERVENTION,
        due_minute=minute,
        priority=0,
        sequence=900_000 + int(hashlib.sha256(event_id.encode()).hexdigest()[:4], 16),
        event_type="effect_program_population",
        payload=payload,
        source_id="ASM-TEST-POPULATION-RULES",
    )


@pytest.fixture
def world(generated_scenario) -> WorldState:
    made = WorldState.from_scenario(generated_scenario)
    assert population.POPULATION_DYNAMICS_STATE_KEY in made.model_state
    made.model_state.setdefault(
        PRESENCE_NAMESPACE,
        PresenceState(
            people={
                key: {
                    "agent_id": key,
                    "household_id": agent.profile.group_id,
                    "status": "admitted",
                    "since_minute": 0,
                    "transition_zone_id": agent.zone_id,
                }
                for key, agent in made.agents.items()
            }
        ).model_dump(mode="json"),
    )
    initialize_population_lifecycle_state(made)
    return made


def _zone(world: WorldState) -> str:
    return next(iter(world.agents.values())).zone_id


def _other_zone(world: WorldState) -> str:
    here = _zone(world)
    return next(zone for zone in sorted(world.zones) if zone != here)


def _occupancy(world: WorldState, zone_id: str) -> int:
    return sum(agent.zone_id == zone_id for agent in world.agents.values())


def _same_as_rule(module: Any, kernels: str, rule_name: str, world: WorldState, *args, **options):
    """Run the rule on a copy and the kernel on the world, and compare them."""
    other = copy.deepcopy(world)
    expected = getattr(module, rule_name).__wrapped__(other, *args, **options)
    result = getattr(native.rules(kernels), rule_name.lstrip("_"))(world, *args, **options)
    assert result == expected
    assert _document(world) == _document(other)
    return result


def _arrive(world: WorldState, count: int = 3, household_size: int = 2, **options: Any) -> int:
    return _same_as_rule(
        population,
        "population",
        "queue_arrivals",
        world,
        event=_event(minute=world.minute_index),
        zone_id=options.pop("zone_id", _zone(world)),
        count=count,
        household_size=household_size,
        **options,
    )


def test_arrivals_with_room_are_the_rule_s_people(world) -> None:
    world.zones[_zone(world)].capacity = None
    assert _arrive(world, count=5) == 5
    state = world.model_state[population.POPULATION_DYNAMICS_STATE_KEY]
    assert len(state["profile_context"]) == 5


@pytest.mark.parametrize(("size", "count"), [(1, 4), (2, 7), (12, 30), (3, 1)])
def test_household_context_matches_the_contract_s_dump(world, size: int, count: int) -> None:
    _arrive(world, count=count, household_size=size)


def test_arrivals_past_capacity_wait_and_are_admitted_when_room_opens(world) -> None:
    zone = _zone(world)
    world.zones[zone].capacity = _occupancy(world, zone) + 1

    assert _arrive(world, count=4) == 1
    state = world.model_state[population.POPULATION_DYNAMICS_STATE_KEY]
    assert len(state["pending_arrivals"]) == 3
    assert state["arrivals"][-1]["waiting_count"] == 3

    # Nobody can come in: nothing changes but the count.
    assert _same_as_rule(population, "population", "admit_pending_arrivals", world, 0) == ()

    world.zones[zone].capacity += 2
    assert len(_same_as_rule(population, "population", "admit_pending_arrivals", world, 0)) == 2
    assert len(state["pending_arrivals"]) == 1
    assert world.metric_state["population_waiting_count"] == 1.0


def test_arrivals_take_an_alternate_zone(world) -> None:
    zone = _zone(world)
    other = _other_zone(world)
    world.zones[zone].capacity = _occupancy(world, zone)
    world.model_configs["geometry_overlay"] = {"schema_version": 3}

    assert _arrive(world, count=2, admission_zone_ids=(other,)) == 2
    assert {
        agent.zone_id for agent in world.agents.values() if agent.id.startswith("arrival-")
    } == {other}


def test_a_person_walking_to_a_zone_holds_their_place(world) -> None:
    other = _other_zone(world)
    zone = _zone(world)
    walker = next(iter(world.agents.values()))
    walker.zone_id = other
    zone = next(agent.zone_id for agent in list(world.agents.values())[1:])
    walker.activity = type(walker.activity).MOVING
    walker.target_id = zone
    world.zones[zone].capacity = _occupancy(world, zone) + 1

    assert not native.rules("population").zone_has_room(world, zone)
    assert not population._zone_has_room.__wrapped__(world, zone)
    assert native.rules("population_lifecycle").zone_has_room(world, zone, excluding=walker.id)
    assert population_lifecycle._zone_has_room.__wrapped__(world, zone, excluding=walker.id)


@pytest.mark.parametrize("extra", [-1, 0, 1, 3])
@pytest.mark.parametrize(
    "spaces", [(), ((2, True),), ((2, False),), ((3, False), (1, True)), ((1, False), (5, False))]
)
def test_room_in_a_zone_with_temporary_spaces(world, extra: int, spaces: Any) -> None:
    from sheltersim.domain.temporary_spaces import (
        TEMPORARY_SPACES_STATE_KEY,
        create_temporary_spaces_state,
    )

    zone = _zone(world)
    state = create_temporary_spaces_state()
    for index, (people, active) in enumerate(spaces):
        state["spaces"][f"space-{index}"] = {
            "space_id": f"space-{index}",
            "zone_id": zone,
            "people_capacity": people,
            "sleeping_capacity": 0,
            "start_minute": 0,
            "end_minute": 10,
            "active": active,
            "start_event_id": "start",
            "end_event_id": None if active else "end",
            "program_id": "program",
            "effect_id": "effect",
            "source_ids": [],
            "geometry_status": "zone_only_capacity",
        }
    world.model_state[TEMPORARY_SPACES_STATE_KEY] = state
    world.zones[zone].capacity = max(1, _occupancy(world, zone) + extra)

    kernels = native.rules("population")
    assert kernels.zone_has_room(world, zone) == population._zone_has_room.__wrapped__(world, zone)
    lifecycle = native.rules("population_lifecycle")
    rule = population_lifecycle._zone_has_room.__wrapped__
    assert lifecycle.zone_has_room(world, zone) == rule(world, zone)
    person = next(iter(world.agents))
    assert lifecycle.zone_has_room(world, zone, excluding=person) == rule(
        world, zone, excluding=person
    )


def test_a_zone_without_capacity_has_room(world) -> None:
    zone = _zone(world)
    world.zones[zone].capacity = None
    assert native.rules("population").zone_has_room(world, zone)
    assert native.rules("population_lifecycle").zone_has_room(world, zone)


def _depart(world: WorldState, count: int = 1, **options: Any) -> int:
    return _same_as_rule(
        population,
        "population",
        "queue_departures",
        world,
        event=_event("departure-event", minute=world.minute_index),
        zone_id=_zone(world),
        count=count,
        **options,
    )


def test_departures_take_the_newest_state_and_admit_who_waits(world) -> None:
    zone = _zone(world)
    world.zones[zone].capacity = _occupancy(world, zone) + 1
    assert _arrive(world, count=3) == 1
    state = world.model_state[population.POPULATION_DYNAMICS_STATE_KEY]
    assert len(state["pending_arrivals"]) == 2

    assert _depart(world, count=1) == 1

    assert len(state["departures"]) == 1
    assert len(state["pending_arrivals"]) == 1


def test_a_departure_of_initial_people_keeps_their_history_and_withdraws_their_events(
    world,
) -> None:
    person = next(iter(world.agents))
    world.schedule_event(_event("mentions-them", minute=5, agent_id=person))
    world.schedule_event(_event("mentions-another", minute=5, agent_id="someone-else"))
    world.schedule_event(_event("mentions-sender", minute=6, sender_agent_id=person))

    count = _occupancy(world, _zone(world))
    assert _depart(world, count=count, include_initial=True) == count

    assert person not in world.agents
    assert {event.event_id for event in world.event_queue.pending()} >= {"mentions-another"}
    assert "mentions-them" not in {event.event_id for event in world.event_queue.pending()}


def test_the_rule_says_why_a_departure_can_not_happen(world) -> None:
    kernels = native.rules("population")
    before = _document(world)
    with pytest.raises(native.Unsupported):
        kernels.queue_departures(world, event=_event(), zone_id=_zone(world), count=5000)
    with pytest.raises(native.Unsupported):
        kernels.queue_departures(world, event=_event(), zone_id="no-such-zone", count=1)
    assert _document(world) == before
    with pytest.raises(EventApplicationError, match="unknown zone"):
        population.queue_departures(world, event=_event(), zone_id="no-such-zone", count=1)


@pytest.mark.parametrize(
    ("options", "text"),
    [
        ({"zone_id": "no-such-zone"}, "unknown zone"),
        ({"count": 0}, "count is outside"),
        ({"household_size": 13}, "household_size is outside"),
        ({"mobility": "flying"}, "mobility is invalid"),
        ({"language": "not a language"}, "language is invalid"),
        ({"admission_zone_ids": ("no-such-zone",)}, "alternate admission zones are invalid"),
        ({"approach_route_id": "no-such-route"}, "approach route is invalid"),
        ({"person_context_overrides": ({"member_index": 9},)}, "person context is invalid"),
    ],
)
def test_a_refused_arrival_changes_nothing_and_raises_the_rule_s_text(
    world, options: dict[str, Any], text: str
) -> None:
    arguments = {"event": _event(), "zone_id": _zone(world), "count": 1, **options}
    before = _document(world)
    with pytest.raises(native.Unsupported):
        native.rules("population").queue_arrivals(world, **arguments)
    assert _document(world) == before
    with pytest.raises(EventApplicationError, match=text):
        population.queue_arrivals(world, **arguments)


def _retry_event(**provenance: Any) -> ScheduledEvent:
    return ScheduledEvent(
        event_id="retried",
        kind=EventKind.INTERVENTION,
        due_minute=0,
        priority=4,
        sequence=880_000,
        event_type="effect_program_population",
        payload={"op": "depart_evacuees", "nested": {"a": [1, 2.5]}},
        source_id="ASM-TEST-POPULATION-RULES",
        provenance=provenance,
    )


@pytest.mark.parametrize(
    "provenance",
    [
        {},
        {"population_retry": {"count": 7}, "other": {"kept": True}},
        {"effect_program_v1": {"program_id": "p", "effect_id": "e"}},
        {"effect_program_v1": {"program_id": "not-in-state", "effect_id": "e"}},
    ],
)
def test_a_retry_is_the_rule_s_event(world, provenance: dict[str, Any]) -> None:
    world.model_state["effect_program_v1"] = {
        "programs": {"p": {"effect_ids": ["e"], "scheduled_event_ids": []}}
    }
    _same_as_rule(
        population,
        "population",
        "schedule_population_retry",
        world,
        _retry_event(**provenance),
        id_prefix="presence-retry",
        subject="population exit",
    )
    pending = world.event_queue.pending()
    retry = next(event for event in pending if event.event_id.startswith("presence-retry-"))
    assert retry.provenance["deferred_from"] == "retried"
    assert dict(retry.payload["nested"])["a"] == (1, 2.5)


def test_the_last_retry_runs_and_the_next_is_the_rule_s_to_refuse(world) -> None:
    _same_as_rule(
        population,
        "population",
        "schedule_population_retry",
        world,
        _retry_event(population_retry={"count": 1439}),
    )
    before = _document(world)
    with pytest.raises(native.Unsupported):
        native.rules("population").schedule_population_retry(
            world, _retry_event(population_retry={"count": 1440})
        )
    assert _document(world) == before


def test_a_retry_at_the_end_of_the_scenario_is_refused(world) -> None:
    world.minute_index = world.duration_minutes - 1
    before = _document(world)
    with pytest.raises(native.Unsupported):
        native.rules("population").schedule_population_retry(world, _retry_event())
    assert _document(world) == before


def _command(world: WorldState, minute: int = 0, **fields: Any) -> dict[str, Any]:
    """Apply one presence command through the kernel and through the rule, on two candidates."""
    world.minute_index = minute
    typed = PopulationPresenceCommand.model_validate(fields)
    ids = typed.selected_ids()
    event = _event(f"command-{fields['action']}-{minute}", minute=minute)
    candidate = copy.deepcopy(world)
    other_world, other = copy.deepcopy(world), copy.deepcopy(world)
    expected = population_lifecycle._apply_presence_action.__wrapped__(
        other_world, other, event, typed, ids
    )
    receipt = native.rules("population_lifecycle").apply_presence_action(
        world, candidate, event, typed, ids
    )
    assert receipt == expected
    assert _document(candidate) == _document(other)
    population_lifecycle.validate_population_lifecycle_state(candidate)
    world.__dict__.clear()
    world.__dict__.update(candidate.__dict__)
    return receipt


def _free_person(world: WorldState) -> str:
    return next(iter(world.agents))


def test_a_person_leaves_and_returns_with_the_same_state(world) -> None:
    person = _free_person(world)
    needs_before = world.agents[person].needs.to_dict()
    zone = world.agents[person].zone_id

    receipt = _command(
        world,
        minute=1,
        action="temporary_exit",
        agent_id=person,
        expected_return_minute=9,
        source_ids=("ASM-TEST-A", "ASM-TEST-B"),
    )
    assert receipt["action"] == "temporary_exit"
    assert person not in world.agents
    presence = world.model_state[PRESENCE_NAMESPACE]["people"][person]
    assert presence["status"] == "temporarily_absent"
    assert presence["expected_return_minute"] == 9
    assert person in world.model_state[POPULATION_LIFECYCLE_STATE_KEY]["people"]

    assert _command(world, minute=2, action="return", agent_id=person)["action"] == "returned"
    assert world.agents[person].zone_id == zone
    assert world.agents[person].needs.to_dict() == needs_before
    assert world.model_state[PRESENCE_NAMESPACE]["people"][person]["status"] == "admitted"


def test_a_return_to_a_full_zone_waits_and_the_rule_admits_it_later(world) -> None:
    person = _free_person(world)
    zone = world.agents[person].zone_id
    _command(world, minute=1, action="transfer", agent_id=person, zone_id=_other_zone(world))
    world.zones[zone].capacity = _occupancy(world, zone)

    receipt = _command(world, minute=2, action="return", agent_id=person, zone_id=zone)
    assert receipt["action"] == "waiting"
    state = world.model_state[POPULATION_LIFECYCLE_STATE_KEY]
    assert [item["agent_id"] for item in state["pending_returns"]] == [person]

    # Still full.
    module_rule = "admit_pending_returns"
    assert _same_as_rule(population_lifecycle, "population_lifecycle", module_rule, world, 3) == ()
    assert person not in world.agents

    world.zones[zone].capacity += 1
    admitted = _same_as_rule(population_lifecycle, "population_lifecycle", module_rule, world, 3)
    assert admitted == (person,)
    assert state["pending_returns"] == []
    assert world.model_state[PRESENCE_NAMESPACE]["people"][person]["status"] == "admitted"
    assert world.model_state[PRESENCE_NAMESPACE]["history"][-1]["action"] == "returned"


def test_a_reunion_and_a_relocation_in_place_are_the_rule_s(world) -> None:
    first, second = [agent.id for agent in world.agents.values() if agent.zone_id == _zone(world)][
        :2
    ]
    receipt = _command(
        world, minute=1, action="reunite", agent_ids=(first, second), household_id="household-new"
    )
    assert receipt["agent_ids"] == sorted((first, second))
    assert (
        native.rules("population_lifecycle").current_household_id(world, first)
        == "household-new"
        == population_lifecycle.current_household_id.__wrapped__(world, first)
    )
    receipt = _command(
        world, minute=2, action="relocate", agent_id=first, zone_id=world.agents[first].zone_id
    )
    assert receipt["action"] == "relocated"


def test_a_relocation_walks_and_its_completion_is_the_rule_s(world) -> None:
    person = _free_person(world)
    origin = world.agents[person].zone_id
    destination = next(
        edge.to_zone_id for edge in world.zone_edges.values() if edge.from_zone_id == origin
    )
    world.zones[destination].capacity = None
    receipt = _command(world, minute=1, action="relocate", agent_id=person, zone_id=destination)
    assert receipt["action"] == "relocation_scheduled"
    assert world.agents[person].activity.value == "moving"
    relocation = world.model_state[POPULATION_LIFECYCLE_STATE_KEY]["pending_relocations"][person]
    event = next(
        item for item in world.event_queue.pending() if item.event_id == relocation["event_id"]
    )

    world.minute_index = event.due_minute
    agent = world.agents[person]
    agent.zone_id = destination
    agent.activity = type(agent.activity).IDLE
    agent.target_id = None
    world.active_tasks.pop(relocation["task_id"])

    _same_as_rule(
        population_lifecycle, "population_lifecycle", "complete_presence_relocation", world, event
    )
    assert person not in world.model_state[POPULATION_LIFECYCLE_STATE_KEY]["pending_relocations"]
    assert world.model_state[PRESENCE_NAMESPACE]["history"][-1]["action"] == "relocated"


@pytest.mark.parametrize(
    "fields",
    [
        {"action": "temporary_exit", "agent_id": "nobody"},
        {"action": "transfer", "agent_id": "nobody"},
        {"action": "return", "agent_id": "nobody"},
        {"action": "relocate", "agent_id": "nobody", "zone_id": "nowhere"},
        {"action": "reunite", "agent_ids": ("nobody", "nor-them"), "household_id": "h"},
    ],
)
def test_a_command_the_rule_refuses_is_refused_with_nothing_changed(world, fields) -> None:
    typed = PopulationPresenceCommand.model_validate(fields)
    event = _event("refused-command")
    candidate = copy.deepcopy(world)
    before = _document(candidate)
    with pytest.raises(native.Unsupported):
        native.rules("population_lifecycle").apply_presence_action(
            world, candidate, event, typed, typed.selected_ids()
        )
    assert _document(candidate) == before
    with pytest.raises(EventApplicationError):
        population_lifecycle._apply_presence_action(
            world, candidate, event, typed, typed.selected_ids()
        )


def _world_with_a_room(scenario: Any) -> WorldState:
    """Everyone full but one surveyed room in the quiet area."""
    document = scenario.model_dump(mode="json")
    for zone in document["zones"]:
        if zone["id"] in {"medical", "quiet"}:
            zone["capacity"] = 0
    made = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    room = TimedAction(
        effect_id="room",
        op="temporary_room_geometry",
        target_selector=TargetSelector(kind="zone", target_id="quiet"),
        start_minute=1,
        end_minute=20,
        parameters={
            "schema_version": 1,
            "room_id": "room",
            "zone_id": "quiet",
            "polygon_m": [
                {"x": 8.0, "y": 2.4},
                {"x": 9.3, "y": 2.4},
                {"x": 9.3, "y": 4.4},
                {"x": 8.0, "y": 4.4},
            ],
            "people_capacity": 4,
            "sleeping_capacity": 0,
            "bed_points_m": [],
            "route_edge_ids": ["route_corridor_quiet", "route_quiet_corridor"],
            "source_ids": ["ASM-TEST-ROOM"],
        },
        units="metre_2d",
        source_id="ASM-TEST-ROOM",
        policy_dependencies=["geometry_overlay_v1"],
        reversible=False,
    )
    schedule_effect_program(
        made,
        EffectProgram(
            program_id="room-program",
            narrative="A surveyed room.",
            source_text="room",
            simulation_start_minute=1,
            effects=[room],
            assumptions=[],
            support_mode=SupportMode.NATIVE,
            compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
        ),
    )
    made.minute_index = 1
    dispatcher = EventDispatcher({"effect_program_geometry": EffectProgramGeometryHandler()})
    assert dispatcher.dispatch_due(made, 1)[0].status.value == "applied"
    return made


def _arrive_in_room(world: WorldState, count: int) -> None:
    arrived = _same_as_rule(
        population,
        "population",
        "queue_arrivals",
        world,
        event=_event(minute=1),
        zone_id="medical",
        count=count,
        admission_zone_ids=("quiet",),
    )
    assert arrived == count


def test_people_given_a_room_in_one_event_stand_apart(generated_scenario) -> None:
    world = _world_with_a_room(generated_scenario)
    _arrive_in_room(world, 3)
    admissions = world.model_state["geometry_overlay_v1"]["room_admissions"]
    assert len(admissions) == 3


def test_a_room_admission_keeps_half_a_metre_from_who_is_there(generated_scenario) -> None:
    probe = _world_with_a_room(generated_scenario)
    _arrive_in_room(probe, 1)
    (person,) = probe.model_state["geometry_overlay_v1"]["room_admissions"]
    point = probe.model_state["movement_v1"]["positions"][person]["point"]

    world = _world_with_a_room(generated_scenario)
    resident = next(iter(world.agents))
    world.model_state["geometry_overlay_v1"]["room_admissions"][resident] = {
        "agent_id": resident,
        "room_id": "room",
        "admitted_minute": 1,
        "event_id": "earlier",
        "source_ids": ["ASM-TEST-ROOM"],
    }
    # Just past the rule's distance, so the same point is still free.
    world.model_state["movement_v1"]["positions"][resident]["point"] = {
        "x": point["x"] + 0.501,
        "y": point["y"],
    }
    _arrive_in_room(world, 1)
    new = next(
        key
        for key in world.model_state["geometry_overlay_v1"]["room_admissions"]
        if key != resident
    )
    assert world.model_state["movement_v1"]["positions"][new]["point"] == point


def test_a_full_presence_history_drops_its_oldest_as_the_rule_does(world) -> None:
    template = {
        "schema_version": 2,
        "event_id": "old-event",
        "minute": 0,
        "action": "arrived",
        "from_zone_id": None,
        "to_zone_id": None,
        "from_household_id": None,
        "to_household_id": None,
        "causation_id": None,
        "source_ids": [],
        "state_snapshot_hash": None,
        "time_semantics": "recorded_event_minute",
    }
    person = next(iter(world.agents))
    world.model_state[PRESENCE_NAMESPACE]["history"] = [
        {**template, "transition_id": f"old-{index}", "agent_id": person} for index in range(20_000)
    ]
    world.zones[_zone(world)].capacity = None
    assert _arrive(world, count=2) == 2
    history = world.model_state[PRESENCE_NAMESPACE]["history"]
    assert len(history) == 20_000
    assert history[0]["transition_id"] == "old-2"


def test_a_full_lifecycle_history_drops_its_oldest_as_the_rule_does(world) -> None:
    state = world.model_state[POPULATION_LIFECYCLE_STATE_KEY]
    state["history"] = [{"event_id": f"old-{index}"} for index in range(20_000)]
    _command(world, minute=1, action="temporary_exit", agent_id=_free_person(world))
    history = world.model_state[POPULATION_LIFECYCLE_STATE_KEY]["history"]
    assert len(history) == 20_000
    assert history[0]["event_id"] == "old-1"


def _break_dynamics_pending(state: dict[str, Any]) -> None:
    state["pending_arrivals"][0]["zone_id"] = "no-such-zone"


def _break_dynamics_traits(state: dict[str, Any]) -> None:
    state["pending_arrivals"][0]["traits"]["age_years"] = 500


def _break_dynamics_context(state: dict[str, Any]) -> None:
    next(iter(state["profile_context"].values()))["source_ids"] = []


def _break_dynamics_field(state: dict[str, Any]) -> None:
    state["pending_arrivals"][0]["something_else"] = 1


@pytest.mark.parametrize(
    "breaks",
    [
        _break_dynamics_pending,
        _break_dynamics_traits,
        _break_dynamics_context,
        _break_dynamics_field,
    ],
)
def test_a_broken_population_state_is_left_to_the_rule_s_text(world, breaks) -> None:
    zone = _zone(world)
    world.zones[zone].capacity = _occupancy(world, zone)
    _arrive(world, count=2)
    kernels = native.rules("population")
    assert kernels.validate_population_dynamics_state(world) is None
    population.validate_population_dynamics_state.__wrapped__(world)

    breaks(world.model_state[population.POPULATION_DYNAMICS_STATE_KEY])

    with pytest.raises(native.Unsupported):
        kernels.validate_population_dynamics_state(world)
    with pytest.raises(ValueError):
        population.validate_population_dynamics_state(world)


def _break_lifecycle_field(state: dict[str, Any]) -> None:
    state["extra"] = 1


def _break_lifecycle_person(state: dict[str, Any]) -> None:
    next(iter(state["people"].values()))["agent"]["stress"] = 2.0


def _break_lifecycle_archive(state: dict[str, Any]) -> None:
    state["relationship_archive"].append({"namespace": "other"})


def _break_lifecycle_return(state: dict[str, Any]) -> None:
    state["pending_returns"].append({"agent_id": "nobody"})


@pytest.mark.parametrize(
    "breaks",
    [
        _break_lifecycle_field,
        _break_lifecycle_person,
        _break_lifecycle_archive,
        _break_lifecycle_return,
    ],
)
def test_a_broken_lifecycle_state_is_left_to_the_rule_s_text(world, breaks) -> None:
    _command(world, minute=1, action="temporary_exit", agent_id=_free_person(world))
    kernels = native.rules("population_lifecycle")
    assert kernels.validate_population_lifecycle_state(world) is None

    breaks(world.model_state[POPULATION_LIFECYCLE_STATE_KEY])

    with pytest.raises(native.Unsupported):
        kernels.validate_population_lifecycle_state(world)
    with pytest.raises(ValueError):
        population_lifecycle.validate_population_lifecycle_state(world)


def test_a_broken_presence_ledger_stops_the_kernels_before_a_write(world) -> None:
    zone = _zone(world)
    world.zones[zone].capacity = _occupancy(world, zone)
    _arrive(world, count=1)
    world.zones[zone].capacity += 1
    world.model_state[PRESENCE_NAMESPACE]["history"].append({"unknown": 1})
    before = _document(world)
    with pytest.raises(native.Unsupported):
        native.rules("population").admit_pending_arrivals(world, 0)
    assert _document(world) == before
