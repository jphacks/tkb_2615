"""rules/overlay.rs and rules/facility_queues.rs against their Python rules, on copies of a world.

The 7-day Run reaches almost none of these, so what is compared here and by the shadow over the
gate is all that is compared (docs/rust-port/lanes/m6-overlay.md). Values next to a limit or a
coefficient are chosen so a change of 1% shows.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.world import WorldState
from sheltersim.domain import facility_queues, geometry_overlay, queueing, temporary_spaces
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.domain.temporary_spaces import create_temporary_spaces_state
from sheltersim.events.models import EventKind, ScheduledEvent

pytestmark = pytest.mark.skipif(not native.available(), reason="the Rust kernels")

ROOM_ZONE = "sleeping"


def _rules(name: str = "overlay") -> Any:
    return native.rules(name)


def _copy(world: WorldState) -> WorldState:
    return WorldState.from_dict(world.to_dict())


def _bytes(world: WorldState) -> str:
    """The state as it is, valid or not: `to_dict` would check it."""
    parts = (
        world.model_state,
        world.metric_state,
        world.id_counters,
        repr(world.agents),
        repr(world.facilities),
        repr(world.staff),
        repr(world.active_tasks),
        repr(world.zone_edges),
        repr(world.event_queue.pending()),
        # A record the log holds as its bytes reads back with its keys sorted.
        [json.dumps(record.to_dict(), sort_keys=True) for record in world.event_log.records],
    )
    return json.dumps(parts, ensure_ascii=False)


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _same(world: WorldState, kernel: Any, rule: Any, *arguments: Any, **options: Any) -> Any:
    """The kernel and the Python rule on two copies: the same result and the same world."""
    theirs = _copy(world)
    ours = _copy(world)
    expected = rule(theirs, *arguments, **options)
    result = kernel(ours, *arguments, **options)
    assert json.dumps(_plain(result)) == json.dumps(_plain(expected))
    assert _bytes(ours) == _bytes(theirs)
    for name in ("geometry_overlay_v1", "temporary_spaces_v1", "facility_queues_v1"):
        mirror = ours.model_state._mirrors.get(name)
        if mirror is not None and name in ours.model_state:
            assert mirror.tainted is None
            assert mirror.verify()
    return result


def _refused(world: WorldState, kernel: Any, rule: Any, *arguments: Any, **options: Any) -> None:
    """Unsupported with nothing changed, where the Python rule raises its own text."""
    before = _bytes(world)
    with pytest.raises(native.Unsupported):
        kernel(world, *arguments, **options)
    assert _bytes(world) == before
    with pytest.raises((ValueError, KeyError, TypeError, AttributeError)):
        rule(_copy(world), *arguments, **options)


def _world(scenario: Any) -> WorldState:
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    return world


@pytest.fixture
def world(generated_scenario: Any) -> WorldState:
    return _world(generated_scenario)


@pytest.fixture
def queued(generated_scenario_with_split_queues: Any) -> WorldState:
    return _world(generated_scenario_with_split_queues)


# ---- Temporary spaces ----------------------------------------------------------------------------


def _space(space_id: str, zone_id: str, *, active: bool = True, people: int = 5) -> dict[str, Any]:
    return {
        "space_id": space_id,
        "zone_id": zone_id,
        "people_capacity": people,
        "sleeping_capacity": 2,
        "start_minute": 0,
        "end_minute": 100,
        "active": active,
        "start_event_id": "start",
        "end_event_id": None if active else "end",
        "program_id": "program",
        "effect_id": "effect",
        "source_ids": ["ASM-A"],
        "geometry_status": "zone_only_capacity",
    }


@pytest.fixture
def spaces(world: WorldState) -> WorldState:
    world.model_state[temporary_spaces.TEMPORARY_SPACES_STATE_KEY] = create_temporary_spaces_state()
    return world


def test_a_space_registers_and_expires_as_the_python_rule_does(spaces: WorldState) -> None:
    zone = next(iter(spaces.zones))
    register = temporary_spaces.register_temporary_space.__wrapped__
    expire = temporary_spaces.expire_temporary_space.__wrapped__
    _same(spaces, _rules().register_temporary_space, register, _space("a", zone), event_id="e1")
    register(spaces, _space("a", zone), event_id="e1")
    assert (
        _same(
            spaces, _rules().expire_temporary_space, expire, "a", event_id="e2", causation_id="e1"
        )
        is True
    )
    assert (
        _same(
            spaces, _rules().expire_temporary_space, expire, "b", event_id="e2", causation_id=None
        )
        is False
    )
    expire(spaces, "a", event_id="e2", causation_id="e1")
    assert (
        _same(
            spaces, _rules().expire_temporary_space, expire, "a", event_id="e3", causation_id=None
        )
        is False
    )


def test_the_limits_of_spaces_are_the_python_rule_s(spaces: WorldState) -> None:
    zone = next(iter(spaces.zones))
    state = spaces.model_state[temporary_spaces.TEMPORARY_SPACES_STATE_KEY]
    register = temporary_spaces.register_temporary_space.__wrapped__
    for index in range(temporary_spaces.MAX_TEMPORARY_SPACES - 1):
        state["spaces"][f"s{index}"] = _space(f"s{index}", zone)
    _same(spaces, _rules().register_temporary_space, register, _space("x", zone), event_id="e")
    state["spaces"]["full"] = _space("full", zone)
    _refused(spaces, _rules().register_temporary_space, register, _space("x", zone), event_id="e")
    _refused(spaces, _rules().register_temporary_space, register, _space("s0", zone), event_id="e")


def test_a_full_history_drops_its_oldest_record(spaces: WorldState) -> None:
    zone = next(iter(spaces.zones))
    state = spaces.model_state[temporary_spaces.TEMPORARY_SPACES_STATE_KEY]
    state["history"].extend(
        {
            "event_id": f"old{index}",
            "space_id": "gone",
            "zone_id": zone,
            "minute": 0,
            "transition": "activated",
            "causation_id": None,
        }
        for index in range(temporary_spaces.MAX_TEMPORARY_SPACE_HISTORY)
    )
    _same(
        spaces,
        _rules().register_temporary_space,
        temporary_spaces.register_temporary_space.__wrapped__,
        _space("a", zone),
        event_id="e",
    )


@pytest.mark.parametrize("occupancy", [None, 0, 3, 10_000])
def test_capacity_is_the_python_rule_s(spaces: WorldState, occupancy: int | None) -> None:
    zone = next(iter(spaces.zones))
    state = spaces.model_state[temporary_spaces.TEMPORARY_SPACES_STATE_KEY]
    state["spaces"]["active"] = _space("active", zone)
    state["spaces"]["old"] = _space("old", zone, active=False, people=7)
    state["spaces"]["other"] = _space("other", "corridor", people=9)
    spaces.zones[zone].capacity = 2
    for name, kernel in (
        ("capacity_metrics", _rules().capacity_metrics),
        ("effective_zone_capacity", _rules().effective_zone_capacity),
        ("effective_total_zone_capacity", _rules().effective_total_zone_capacity),
    ):
        rule = getattr(temporary_spaces, name).__wrapped__
        assert kernel(spaces, zone, occupancy=occupancy) == rule(spaces, zone, occupancy=occupancy)
    spaces.zones[zone].capacity = None
    assert _rules().capacity_metrics(
        spaces, zone, occupancy=occupancy
    ) == temporary_spaces.capacity_metrics.__wrapped__(spaces, zone, occupancy=occupancy)


def test_capacity_counts_rooms_by_the_python_rule(world: WorldState) -> None:
    room = _room_world(world, people=3)
    state = world.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]
    state["room_admissions"]["A001"] = {
        "agent_id": "A001",
        "room_id": room,
        "admitted_minute": 0,
        "event_id": "e",
        "source_ids": [],
    }
    world.zones[ROOM_ZONE].capacity = 2
    rule = temporary_spaces.capacity_metrics.__wrapped__
    assert _rules().capacity_metrics(world, ROOM_ZONE, occupancy=9) == rule(
        world, ROOM_ZONE, occupancy=9
    )
    state["temporary_rooms"][room]["active"] = False
    state["temporary_rooms"][room]["end_event_id"] = "end"
    assert _rules().capacity_metrics(world, ROOM_ZONE, occupancy=9) == rule(
        world, ROOM_ZONE, occupancy=9
    )
    world.model_configs["geometry_overlay"]["schema_version"] = 2
    state["temporary_rooms"][room]["active"] = True
    state["temporary_rooms"][room]["end_event_id"] = None
    assert _rules().capacity_metrics(world, ROOM_ZONE, occupancy=9) == rule(
        world, ROOM_ZONE, occupancy=9
    )


def test_incoming_travelers_are_the_python_rule_s(world: WorldState) -> None:
    from sheltersim.domain.models import Activity

    first, second = list(world.agents)[:2]
    world.agents[first].activity = Activity.MOVING
    world.agents[first].target_id = ROOM_ZONE
    toilet = next(key for key, item in world.facilities.items() if "toilet" in item.kind)
    world.agents[second].activity = Activity.MOVING
    world.agents[second].target_id = toilet
    rule = temporary_spaces.incoming_travelers.__wrapped__
    for zone in (ROOM_ZONE, world.facilities[toilet].zone_id, "corridor"):
        for excluding in (None, first, second):
            assert _rules().incoming_travelers(world, zone, excluding=excluding) == rule(
                world, zone, excluding=excluding
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("people_capacity", 0),
        ("people_capacity", 1001),
        ("sleeping_capacity", 6),
        ("sleeping_capacity", -1),
        ("start_minute", -1),
        ("end_minute", 0),
        ("start_event_id", ""),
        ("end_event_id", "end"),
        ("program_id", 1),
        ("source_ids", [""]),
        ("geometry_status", "room"),
        ("zone_id", "nowhere"),
        ("active", 1),
    ],
)
def test_the_check_of_spaces_takes_nothing_the_python_rule_refuses(
    spaces: WorldState, field: str, value: Any
) -> None:
    zone = next(iter(spaces.zones))
    state = spaces.model_state[temporary_spaces.TEMPORARY_SPACES_STATE_KEY]
    state["spaces"]["a"] = _space("a", zone)
    assert _rules().validate_spaces(spaces) is True
    temporary_spaces.validate_temporary_spaces_state(spaces)
    state["spaces"]["a"][field] = value
    with pytest.raises(ValueError):
        temporary_spaces.validate_temporary_spaces_state(spaces)
    with pytest.raises(native.Unsupported):
        _rules().validate_spaces(spaces)


# ---- Rooms and beds ------------------------------------------------------------------------------


def _room_record(
    world: WorldState, room_id: str, *, people: int = 2, beds: int = 1
) -> dict[str, Any]:
    geometry = world.model_configs["facility_geometry"]
    sleeping = next(zone for zone in geometry["zones"] if zone["zone_id"] == ROOM_ZONE)
    left = min(point["x"] for point in sleeping["polygon"])
    bottom = max(point["y"] for point in sleeping["polygon"])
    return {
        "schema_version": 1,
        "room_id": room_id,
        "zone_id": ROOM_ZONE,
        "polygon_m": [
            {"x": left + 0.5, "y": bottom - 4.4},
            {"x": left + 2.5, "y": bottom - 4.4},
            {"x": left + 2.5, "y": bottom - 0.4},
            {"x": left + 0.5, "y": bottom - 0.4},
        ],
        "people_capacity": people,
        "sleeping_capacity": beds,
        "bed_points_m": [
            {"x": left + 1.5, "y": bottom - 2.4 + 0.8 * index} for index in range(beds)
        ],
        "route_edge_ids": ["route_corridor_sleeping", "route_sleeping_corridor"],
        "source_ids": ["ASM-TEMP-ROOM-TEST-V1"],
        "start_minute": 0,
        "end_minute": world.duration_minutes,
        "active": True,
        "start_event_id": "start",
        "end_event_id": None,
        "program_id": "program",
        "effect_id": "effect",
    }


def _room_world(world: WorldState, *, people: int = 2, beds: int = 1, room_id: str = "room") -> str:
    state = world.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]
    state["temporary_rooms"][room_id] = _room_record(world, room_id, people=people, beds=beds)
    return room_id


def _person_in_room_zone(world: WorldState) -> str:
    return next(key for key, agent in world.agents.items() if agent.zone_id == ROOM_ZONE)


def test_a_bed_is_reserved_as_the_python_rule_does(world: WorldState) -> None:
    _room_world(world, people=4, beds=3)
    agent_id = _person_in_room_zone(world)
    rule = geometry_overlay.reserve_sleep_bed.__wrapped__
    result = _same(world, _rules().reserve_sleep_bed, rule, agent_id)
    assert result is not None and result["room_id"] == "room"
    # Who holds a bed keeps it, with the minute it was reserved.
    rule(world, agent_id)
    world.minute_index = 3
    again = _same(world, _rules().reserve_sleep_bed, rule, agent_id)
    assert again["reserved_minute"] == 0


def test_no_bed_is_none_as_the_python_rule_says(world: WorldState) -> None:
    agent_id = _person_in_room_zone(world)
    rule = geometry_overlay.reserve_sleep_bed.__wrapped__
    assert _same(world, _rules().reserve_sleep_bed, rule, agent_id) is None
    _room_world(world, people=2, beds=1)
    other = next(key for key, agent in world.agents.items() if key != agent_id)
    world.agents[other].zone_id = "corridor"
    world.model_state["movement_v1"]["positions"][other]["zone_id"] = "corridor"
    assert _same(world, _rules().reserve_sleep_bed, rule, other) is None
    world.model_configs["geometry_overlay"]["schema_version"] = 1
    assert _same(world, _rules().reserve_sleep_bed, rule, agent_id) is None


@pytest.mark.parametrize(
    ("offset", "taken"), [(0.0, True), (0.7499, True), (0.7500001, False), (1.2, False)]
)
def test_someone_at_a_bed_is_using_it_within_its_radius(
    world: WorldState, offset: float, taken: bool
) -> None:
    room = _room_record(world, "room")
    world.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]["temporary_rooms"]["room"] = room
    bed = room["bed_points_m"][0]
    people = [key for key, agent in world.agents.items() if agent.zone_id == ROOM_ZONE]
    first, second = people[:2]
    world.model_state["movement_v1"]["positions"][second]["point"] = {
        "x": bed["x"] + offset,
        "y": bed["y"],
    }
    rule = geometry_overlay.reserve_sleep_bed.__wrapped__
    result = _same(world, _rules().reserve_sleep_bed, rule, first)
    assert (result is None) is taken


def test_a_reservation_taken_by_another_is_passed_by(world: WorldState) -> None:
    _room_world(world, people=4, beds=2)
    state = world.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]
    people = [key for key, agent in world.agents.items() if agent.zone_id == ROOM_ZONE]
    geometry_overlay.reserve_sleep_bed.__wrapped__(world, people[0])
    held = state["bed_reservations"][people[0]]["bed_index"]
    rule = geometry_overlay.reserve_sleep_bed.__wrapped__
    result = _same(world, _rules().reserve_sleep_bed, rule, people[1])
    assert result["bed_index"] != held


@pytest.mark.parametrize("held", [True, False])
def test_a_release_is_the_python_rule_s(world: WorldState, held: bool) -> None:
    _room_world(world, people=4, beds=2)
    agent_id = _person_in_room_zone(world)
    if held:
        geometry_overlay.reserve_sleep_bed.__wrapped__(world, agent_id)
    assert (
        _same(
            world,
            _rules().release_sleep_bed,
            geometry_overlay.release_sleep_bed.__wrapped__,
            agent_id,
        )
        is held
    )


def test_a_room_s_intake_is_the_python_rule_s(world: WorldState) -> None:
    room = _room_world(world, people=2, beds=1)
    point_rule = geometry_overlay.room_admission_point.__wrapped__
    room_rule = geometry_overlay.available_temporary_room.__wrapped__
    point = _rules().room_admission_point(world, room)
    assert point == point_rule(world, room) and point is not None
    assert (
        _rules().available_temporary_room(world, ROOM_ZONE) == room_rule(world, ROOM_ZONE) == room
    )
    admit = geometry_overlay.admit_to_temporary_room.__wrapped__
    first, second, third = list(world.agents)[:3]
    options = {"room_id": room, "minute": 0, "event_id": "e1"}
    _same(world, _rules().admit_to_temporary_room, admit, agent_id=first, **options)
    admit(world, agent_id=first, **options)
    # Someone standing there takes the point within half a metre, and not beyond it.
    position = world.model_state["movement_v1"]["positions"][first]
    for offset in (0.0, 0.4999, 0.5, 0.6):
        position["point"] = {"x": point.x + offset, "y": point.y}
        assert _rules().room_admission_point(world, room) == point_rule(world, room)
    admit(world, agent_id=second, **options)
    assert (
        _rules().available_temporary_room(world, ROOM_ZONE) == room_rule(world, ROOM_ZONE) is None
    )
    release = geometry_overlay.release_temporary_room_admission.__wrapped__
    assert (
        _same(world, _rules().release_temporary_room_admission, release, first) is True
        and _same(world, _rules().release_temporary_room_admission, release, third) is False
    )
    closed = world.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]["temporary_rooms"][room]
    closed["active"] = False
    closed["end_event_id"] = "end"
    _refused(world, _rules().admit_to_temporary_room, admit, agent_id=third, **options)


def test_a_large_room_inspects_as_many_points_as_the_python_rule(world: WorldState) -> None:
    room = _room_world(world, people=2, beds=0)
    geometry = world.model_configs["facility_geometry"]
    sleeping = next(zone for zone in geometry["zones"] if zone["zone_id"] == ROOM_ZONE)
    state = world.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]
    state["temporary_rooms"][room]["polygon_m"] = deepcopy(sleeping["polygon"])
    rule = geometry_overlay.room_admission_point.__wrapped__
    assert _rules().room_admission_point(world, room) == rule(world, room)


# ---- A facility placed during a Run --------------------------------------------------------------


def test_a_placement_is_the_python_rule_s(world: WorldState) -> None:
    from sheltersim.domain.models import FacilityState

    for index in range(4):
        facility_id = f"extra_{index}"
        world.facilities[facility_id] = FacilityState(
            id=facility_id,
            kind="toilet",
            zone_id="toilet",
            capacity=1,
            working_units=1,
        )
        assert _rules().installed_facility_point(
            world, facility_id
        ) == geometry_overlay.installed_facility_point.__wrapped__(world, facility_id)
        _same(
            world,
            _rules().place_installed_facility,
            geometry_overlay.place_installed_facility.__wrapped__,
            facility_id,
            event_id=f"e{index}",
            source_ids=("ASM-A", "ASM-A"),
        )
        geometry_overlay.place_installed_facility.__wrapped__(
            world, facility_id, event_id=f"e{index}", source_ids=("ASM-A",)
        )
    assert _rules().installed_facility_placement_enabled(world) is True
    world.model_configs["geometry_overlay"]["schema_version"] = 3
    assert _rules().installed_facility_placement_enabled(world) is False


def test_no_free_point_is_refused_and_the_python_rule_says_so(world: WorldState) -> None:
    from sheltersim.domain.models import FacilityState

    # 16 more than fit: the search gives up where the Python rule does.
    for index in range(5):
        facility_id = f"extra_{index}"
        world.facilities[facility_id] = FacilityState(
            id=facility_id, kind="toilet", zone_id="toilet", capacity=1, working_units=1
        )
        point = geometry_overlay.installed_facility_point.__wrapped__(world, facility_id)
        assert _rules().installed_facility_point(world, facility_id) == point
        if point is None:
            _refused(
                world,
                _rules().place_installed_facility,
                geometry_overlay.place_installed_facility.__wrapped__,
                facility_id,
                event_id="e",
                source_ids=(),
            )
            return
        geometry_overlay.place_installed_facility.__wrapped__(
            world, facility_id, event_id=f"e{index}", source_ids=()
        )
    pytest.fail("the zone had room for every toilet")


# ---- Changes of the floor plan -------------------------------------------------------------------


def _event(
    world: WorldState,
    op: str,
    target: str,
    parameters: Any,
    end: int | None,
    event_id: str = "change-1",
) -> Any:
    return ScheduledEvent(
        event_id=event_id,
        kind=EventKind.ENDOGENOUS,
        due_minute=world.minute_index,
        priority=0,
        sequence=900,
        event_type="effect_program_geometry",
        payload={
            "program_id": "program",
            "effect_id": "effect",
            "modifier_id": "modifier",
            "target_id": target,
            "op": op,
            "parameters": parameters,
            "end_minute": end,
        },
        source_id="ASM-SOURCE",
        causation_id="cause",
        provenance={"effect_program_v1": {"assumption_ids": ["ASM-ASSUME", "ASM-SOURCE", ""]}},
    )


def _corner(world: WorldState, zone_id: str, *, x: float = 1.0, y: float = 1.0) -> list[dict]:
    geometry = world.model_configs["facility_geometry"]
    zone = next(item for item in geometry["zones"] if item["zone_id"] == zone_id)
    left = min(point["x"] for point in zone["polygon"]) + x
    top = min(point["y"] for point in zone["polygon"]) + y
    return [
        {"x": left, "y": top},
        {"x": left + 0.4, "y": top},
        {"x": left + 0.4, "y": top + 0.4},
        {"x": left, "y": top + 0.4},
    ]


def _change_cases(world: WorldState) -> list[tuple[str, str, Any, int | None]]:
    toilet = next(key for key, item in world.facilities.items() if item.kind == "toilet")
    geometry = world.model_configs["facility_geometry"]
    point = geometry["facility_points"][toilet]
    room = {
        key: value
        for key, value in _room_record(world, "new-room").items()
        if key
        in {
            "schema_version",
            "room_id",
            "zone_id",
            "polygon_m",
            "people_capacity",
            "sleeping_capacity",
            "bed_points_m",
            "route_edge_ids",
            "source_ids",
        }
    }
    return [
        ("set_route_access", "route_corridor_sleeping", {"accessible": False}, 50),
        ("set_route_access", "route_corridor_sleeping", {"accessible": True}, 50),
        ("set_route_access", "route_corridor_sleeping", {"accessible": False}, None),
        ("set_route_access", "no_route", {"accessible": False}, 50),
        ("move_facility", toilet, {"point_m": {"x": point["x"] + 0.1, "y": point["y"]}}, None),
        ("move_facility", toilet, {"point_m": {"x": point["x"] + 0.1, "y": point["y"]}}, 50),
        ("move_facility", toilet, {"point_m": {"x": -1.0, "y": point["y"]}}, None),
        ("move_facility", "no_facility", {"point_m": {"x": 1.0, "y": 1.0}}, None),
        ("set_partition", ROOM_ZONE, {"polygon_m": _corner(world, ROOM_ZONE)}, 50),
        ("set_partition", ROOM_ZONE, {"polygon_m": _corner(world, ROOM_ZONE)}, None),
        ("set_partition", "no_zone", {"polygon_m": _corner(world, ROOM_ZONE)}, 50),
        ("temporary_room_geometry", ROOM_ZONE, room, 50),
        ("temporary_room_geometry", ROOM_ZONE, room, None),
        ("temporary_room_geometry", "corridor", room, 50),
        (
            "transfer_equipment",
            toilet,
            {
                "schema_version": 1,
                "equipment_id": toilet,
                "from_zone_id": world.facilities[toilet].zone_id,
                "to_zone_id": "sleeping",
                "point_m": {"x": 5.0, "y": 9.0},
            },
            None,
        ),
        ("no_such_operator", ROOM_ZONE, {}, 50),
    ]


def test_each_change_is_checked_and_applied_as_the_python_rule_does(world: WorldState) -> None:
    applied = 0
    for op, target, parameters, end in _change_cases(world):
        rule = geometry_overlay.validate_geometry_change.__wrapped__
        try:
            rule(_copy(world), op, target, parameters, end)
        except ValueError:
            _refused(world, _rules().validate_geometry_change, rule, op, target, parameters, end)
            event = _event(world, op, target, parameters, end)
            _refused(
                world,
                _rules().apply_geometry_change,
                geometry_overlay.apply_geometry_change.__wrapped__,
                event,
            )
            continue
        _rules().validate_geometry_change(world, op, target, parameters, end)
        event = _event(world, op, target, parameters, end)
        _same(
            world,
            _rules().apply_geometry_change,
            geometry_overlay.apply_geometry_change.__wrapped__,
            event,
        )
        applied += 1
    assert applied >= 5


@pytest.mark.parametrize("op", ["set_partition", "set_route_access", "temporary_room_geometry"])
def test_a_change_expires_as_the_python_rule_does(world: WorldState, op: str) -> None:
    case = next(item for item in _change_cases(world) if item[0] == op and item[3] == 50)
    _, target, parameters, end = case
    event = _event(world, op, target, parameters, end)
    rule = geometry_overlay.apply_geometry_change.__wrapped__
    rule(world, event)
    world.validate()
    ending = _event(world, op, target, parameters, end, event_id="change-2")
    _same(world, _rules().apply_geometry_change, rule, ending, expire=True)


def test_a_moved_facility_keeps_its_plan_in_rust(world: WorldState) -> None:
    """A move changes the plan, its service points and footprints, for the rules that read them."""
    toilet = next(key for key, item in world.facilities.items() if item.kind == "toilet")
    point = world.model_configs["facility_geometry"]["facility_points"][toilet]
    event = _event(
        world,
        "move_facility",
        toilet,
        {"point_m": {"x": point["x"] + 0.1, "y": point["y"]}},
        None,
    )
    _rules().apply_geometry_change(world, event)
    agent_id = next(iter(world.agents))
    rule = geometry_overlay.effective_geometry(world)
    assert rule.facility_points[toilet].x == pytest.approx(point["x"] + 0.1)
    movement = native.rules("movement")
    world.agents[agent_id].zone_id = world.facilities[toilet].zone_id
    from sheltersim.domain.movement import facility_approach_route

    found = movement.facility_approach_route(world, agent_id, toilet)
    expected = facility_approach_route.__wrapped__(world, agent_id, toilet)
    assert repr(found) == repr(expected)


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda state: state.__setitem__("schema_version", 2),
        lambda state: state.__setitem__("extra", 1),
        lambda state: state["facility_points"].__setitem__("nobody", {"point_m": {}}),
        lambda state: state["temporary_rooms"]["room"].__setitem__("active", 1),
        lambda state: state["temporary_rooms"]["room"].__setitem__("zone_id", "nowhere"),
        lambda state: state["temporary_rooms"]["room"].__setitem__("people_capacity", 0),
        lambda state: state["temporary_rooms"]["room"].__setitem__("start_minute", 99),
        lambda state: state["temporary_rooms"]["room"].__setitem__("end_event_id", "x"),
        lambda state: state["temporary_rooms"]["room"].__setitem__("route_edge_ids", ["no"]),
        lambda state: state["bed_reservations"].__setitem__("nobody", {}),
        lambda state: state["room_admissions"].__setitem__("nobody", {}),
        lambda state: state["history"].append({"transition_id": "x"}),
        lambda state: state["partitions"].__setitem__("p", {"zone_id": "nowhere"}),
        lambda state: state["route_modifiers"].__setitem__("no", {}),
    ],
)
def test_the_check_of_the_overlay_takes_nothing_the_python_rule_refuses(
    world: WorldState, corrupt: Any
) -> None:
    _room_world(world)
    state = world.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]
    assert _rules().validate_overlay(world) is True
    geometry_overlay.validate_geometry_overlay_state(world)
    corrupt(state)
    with pytest.raises(ValueError):
        geometry_overlay.validate_geometry_overlay_state(world)
    with pytest.raises(native.Unsupported):
        _rules().validate_overlay(world)


def test_a_bed_is_checked_against_its_target(world: WorldState) -> None:
    _room_world(world, people=4, beds=2)
    agent_id = _person_in_room_zone(world)
    geometry_overlay.reserve_sleep_bed.__wrapped__(world, agent_id)
    assert _rules().validate_overlay(world) is True
    state = world.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]
    state["bed_reservations"][agent_id]["target_id"] = "bed-000000000000000000000000"
    with pytest.raises(ValueError):
        geometry_overlay.validate_geometry_overlay_state(world)
    with pytest.raises(native.Unsupported):
        _rules().validate_overlay(world)


# ---- The temporary-room branch of a decision -----------------------------------------------------


def _controller(world: WorldState) -> Any:
    from sheltersim.experiments.runner import OfflineSimulationController

    class _Sink:
        def __getattr__(self, name: str) -> Any:
            return lambda *arguments, **options: None

    return OfflineSimulationController(world, _Sink())


def _sleep_proposal(world: WorldState, agent_id: str) -> Any:
    from types import SimpleNamespace

    from sheltersim.policies.behavior import ActionKind

    return SimpleNamespace(
        agent_id=agent_id,
        action=ActionKind.SLEEP,
        target_id=None,
        next_decision_at=world.minute_index + 5,
    )


@pytest.mark.parametrize("at_bed", [False, True])
def test_sleep_in_a_room_is_the_python_step_s(world: WorldState, at_bed: bool) -> None:
    from sheltersim.experiments.phases.propose import ProposePhase

    room = _room_record(world, "room")
    world.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]["temporary_rooms"]["room"] = room
    agent_id = _person_in_room_zone(world)
    if at_bed:
        world.model_state["movement_v1"]["positions"][agent_id]["point"] = dict(
            room["bed_points_m"][0]
        )
    ours, theirs = _copy(world), _copy(world)
    theirs_result = ProposePhase._apply_effect(
        _controller(theirs), _sleep_proposal(theirs, agent_id)
    )
    kernel = native.rules("proposals").apply_effect
    result = kernel(_controller(ours), _sleep_proposal(ours, agent_id))
    assert tuple(result) == tuple(theirs_result)
    assert _bytes(ours) == _bytes(theirs)
    assert ours.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]["bed_reservations"]
    assert result[0] is not None
    assert [repr(event) for event in ours.event_queue.pending()] == [
        repr(event) for event in theirs.event_queue.pending()
    ]


def test_sleep_past_the_run_s_end_releases_the_bed(world: WorldState) -> None:
    from sheltersim.experiments.phases.propose import ProposePhase

    _room_world(world)
    agent_id = _person_in_room_zone(world)
    # The walk would end after the Run: no trip, and what the person held is gone.
    geometry_overlay.reserve_sleep_bed.__wrapped__(world, agent_id)
    world.minute_index = world.duration_minutes - 1
    ours, theirs = _copy(world), _copy(world)
    expected = ProposePhase._apply_effect(_controller(theirs), _sleep_proposal(theirs, agent_id))
    result = native.rules("proposals").apply_effect(
        _controller(ours), _sleep_proposal(ours, agent_id)
    )
    assert tuple(result) == tuple(expected)
    assert _bytes(ours) == _bytes(theirs)
    assert not ours.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]["bed_reservations"]


# ---- Independent queues --------------------------------------------------------------------------


def _queue_ids(world: WorldState) -> list[str]:
    return sorted(world.model_state[facility_queues.FACILITY_QUEUES_STATE_KEY]["queues"])


def _line_up(world: WorldState, facility_id: str, queue_id: str, agents: list[str]) -> list[str]:
    zone = world.facilities[facility_id].zone_id
    service = ToiletServiceManager(world, min_service_minutes=2, max_service_minutes=2)
    tasks = []
    for agent_id in agents:
        world.agents[agent_id].zone_id = zone
        position = world.model_state["movement_v1"]["positions"].get(agent_id)
        if position is not None:
            position["zone_id"] = zone
        receipt = service.request(agent_id, facility_id, queue_id=queue_id, validate_state=False)
        tasks.append(receipt.queue_task_id)
    return tasks


def test_the_row_of_the_queues_is_its_tree(queued: WorldState) -> None:
    mirror = queued._core.mirror(facility_queues.FACILITY_QUEUES_STATE_KEY)
    assert mirror.has_row
    _line_up(queued, "toilet_01", "toilet_01-left-fifo", ["A001", "A002"])
    assert mirror.tainted is None
    assert mirror.verify()
    assert mirror.row_json() == native.canonical_json(
        queued.model_state[facility_queues.FACILITY_QUEUES_STATE_KEY]
    )
    queued.model_state[facility_queues.FACILITY_QUEUES_STATE_KEY]["queues"]["toilet_01-left-fifo"][
        "last_stall"
    ] = 7
    assert mirror.tainted is not None
    with pytest.raises(native.Unsupported):
        _rules("facility_queues").queue_agent_ids(queued, "toilet_01-left-fifo")


def test_reads_of_the_queues_are_the_python_rule_s(queued: WorldState) -> None:
    rules = _rules("facility_queues")
    tasks = _line_up(queued, "toilet_01", "toilet_01-left-fifo", ["A001", "A002"])
    _line_up(queued, "toilet_01", "toilet_01-right-fifo", ["A003"])

    def rule(name: str) -> Any:
        return getattr(facility_queues, name).__wrapped__

    queue = "toilet_01-left-fifo"
    assert rules.queue_agent_ids(queued, queue) == rule("queue_agent_ids")(queued, queue)
    assert rules.queue_agent_ids(queued, "none") == []
    for facility in queued.facilities:
        assert rules.facility_has_independent_queues(queued, facility) == rule(
            "facility_has_independent_queues"
        )(queued, facility)
        assert rules.queue_ids_for_facility(queued, facility) == rule("queue_ids_for_facility")(
            queued, facility
        )
    assert rules.queue_head_agent_ids(queued, "toilet_01") == rule("queue_head_agent_ids")(
        queued, "toilet_01"
    )
    assert rules.request_for_task(queued, tasks[0]) == rule("request_for_task")(queued, tasks[0])
    assert rules.request_for_task(queued, "none") is None
    for options in ({}, {"queue_id": queue}, {"queue_id": "toilet_01-right-fifo"}):
        assert rules.request_for_agent(queued, "A002", **options) == rule("request_for_agent")(
            queued, "A002", **options
        )
    assert rules.create_facility_queue_state(queued) == rule("create_facility_queue_state")(queued)
    assert rules.independent_queues_required(queued.model_configs) is True
    assert rules.is_legacy_queue_checkpoint(queued) is False


def test_the_shortest_accessible_queue_is_the_python_rule_s(queued: WorldState) -> None:
    rule = facility_queues.choose_queue_id.__wrapped__
    kernel = _rules("facility_queues").choose_queue_id
    assert kernel(queued, "toilet_01", "A005") == rule(queued, "toilet_01", "A005")
    _line_up(queued, "toilet_01", "toilet_01-left-fifo", ["A001"])
    assert kernel(queued, "toilet_01", "A005") == rule(queued, "toilet_01", "A005")
    _line_up(queued, "toilet_01", "toilet_01-right-fifo", ["A002", "A003"])
    assert kernel(queued, "toilet_01", "A005") == rule(queued, "toilet_01", "A005")
    # Without the access its point asks for, a person doesn't count that queue.
    geometry = queued.model_configs["facility_geometry"]
    for point in geometry["service_points"]:
        if point["queue_id"] == "toilet_01-left-fifo":
            point["access"] = {
                "accessibility": "restricted",
                "required_access_tags": ["key_holder"],
                "required_staff_skills": [],
            }
    assert kernel(queued, "toilet_01", "A005") == rule(queued, "toilet_01", "A005")
    assert kernel(queued, "water_station_main", "A005") == rule(
        queued, "water_station_main", "A005"
    )


def test_writes_of_the_queues_are_the_python_rule_s(queued: WorldState) -> None:
    rules = _rules("facility_queues")
    queue = "toilet_01-left-fifo"
    tasks = _line_up(queued, "toilet_01", queue, ["A001", "A002"])
    state = queued.model_state[facility_queues.FACILITY_QUEUES_STATE_KEY]
    enqueue = facility_queues.enqueue.__wrapped__
    options = {
        "queue_id": queue,
        "request_id": "fresh",
        "task_id": "fresh-task",
        "agent_id": "A003",
        "service_kind": "toilet",
        "requested_minute": 0,
    }
    _same(queued, rules.enqueue, enqueue, **options)
    _refused(queued, rules.enqueue, enqueue, **{**options, "request_id": tasks[0]})
    _refused(queued, rules.enqueue, enqueue, **{**options, "queue_id": "none"})
    _same(
        queued, rules.mark_stall, facility_queues.mark_stall.__wrapped__, queue, tasks[0], "staff"
    )
    _same(
        queued, rules.mark_stall, facility_queues.mark_stall.__wrapped__, "none", tasks[0], "staff"
    )
    _same(
        queued, rules.start_service, facility_queues.start_service.__wrapped__, tasks[0], "active"
    )
    for status in ("completed", "abandoned"):
        _same(
            queued,
            rules.finish_service,
            facility_queues.finish_service.__wrapped__,
            tasks[1],
            status=status,
        )
    assert (
        _same(
            queued,
            rules.finish_service,
            facility_queues.finish_service.__wrapped__,
            "none",
            status="x",
        )
        is None
    )
    request = state["requests"][tasks[0]]
    for transition, wait, cause in (("queued", None, None), ("service_started", 0, "cause")):
        _same(
            queued,
            rules.record_queue_transition,
            facility_queues.record_queue_transition.__wrapped__,
            request,
            transition=transition,
            task_id=tasks[0],
            wait_minutes=wait,
            causation_id=cause,
        )


def test_the_check_of_the_queues_takes_nothing_the_python_rule_refuses(queued: WorldState) -> None:
    rules = _rules("facility_queues")
    _line_up(queued, "toilet_01", "toilet_01-left-fifo", ["A001"])
    assert rules.validate(queued) is True
    facility_queues.validate_facility_queue_state(queued)
    state = queued.model_state[facility_queues.FACILITY_QUEUES_STATE_KEY]
    request_id = next(iter(state["requests"]))
    row = state["queues"]["toilet_01-left-fifo"]

    def refuses() -> bool:
        with pytest.raises(ValueError):
            facility_queues.validate_facility_queue_state(queued)
        try:
            return rules.validate(queued) is False
        except native.Unsupported:
            return True

    state["requests"][request_id]["requested_minute"] = 10_000
    assert refuses()
    state["requests"][request_id]["requested_minute"] = 0
    state["requests"][request_id]["service_kind"] = "water"
    assert refuses()
    state["requests"][request_id]["service_kind"] = "toilet"
    state["requests"][request_id]["status"] = "active"
    assert refuses()
    state["requests"][request_id]["status"] = "queued"
    row["active_request_ids"].append(request_id)
    assert refuses()
    row["active_request_ids"].clear()
    row["last_stall"] = {"minute": -1, "request_id": request_id, "reason": "staff"}
    assert refuses()
    row["last_stall"] = None
    assert rules.validate(queued) is True
    state["requests"]["unlisted"] = dict(state["requests"][request_id], request_id="unlisted")
    assert refuses()


def test_a_fifo_keeps_its_order_and_only_a_wait_ends_it(queued: WorldState) -> None:
    queues = _rules("queues")
    tasks = _line_up(queued, "toilet_01", "toilet_01-left-fifo", ["A001", "A002"])
    prepare = queues.prepare_independent_facility_queue
    rule = queueing.prepare_independent_facility_queue.__wrapped__
    _same(queued, prepare, rule, "toilet_01-left-fifo", minute=queued.minute_index, resource=False)
    _same(queued, prepare, rule, "none", minute=queued.minute_index, resource=False)
    # Past the toilet's default wait, with a low need: the kernel leaves it to the Python rule.
    queued.agents["A002"].needs.toilet_urgency = 0.1
    queued.active_tasks[tasks[1]].start_minute = 0
    queued.minute_index = 121
    before = _bytes(queued)
    with pytest.raises(native.Unsupported):
        prepare(queued, "toilet_01-left-fifo", minute=121, resource=False)
    assert _bytes(queued) == before
    queued.minute_index = 119
    _same(queued, prepare, rule, "toilet_01-left-fifo", minute=119, resource=False)


@pytest.mark.parametrize("queue_id", ["toilet_01-left-fifo", "toilet_01-right-fifo"])
def test_walks_of_a_fifo_are_the_python_rule_s(queued: WorldState, queue_id: str) -> None:
    from sheltersim.domain import movement

    queues = _rules("queues")
    _line_up(queued, "toilet_01", queue_id, ["A001", "A002", "A003"])
    minute = queued.minute_index
    options = {"resource": False, "minute": minute, "queue_id": queue_id}
    first = _same(
        queued,
        queues.progress_physical_queue_positions,
        movement.progress_physical_queue_positions.__wrapped__,
        "toilet_01",
        **options,
    )
    journey_world = _copy(queued)
    queues.progress_physical_queue_positions(journey_world, "toilet_01", **options)
    journeys = journey_world.model_state["movement_v1"]["journeys"]
    assert all(journey.get("queue_id") == queue_id for journey in journeys.values())
    assert first in (True, False)
    _same(
        queued,
        queues.progress_physical_queue_head_to_service,
        movement.progress_physical_queue_head_to_service.__wrapped__,
        "toilet_01",
        **options,
    )
    _same(
        queued,
        queues.progress_physical_queue_head_to_service,
        movement.progress_physical_queue_head_to_service.__wrapped__,
        "toilet_01",
        resource=False,
        minute=minute,
    )
    world_move = native.rules("movement")
    geometry = geometry_overlay.effective_geometry(queued)
    for agent_id in ("A001", "A002", "A003"):
        found = world_move.queue_slot_position(queued, geometry, agent_id, "toilet_01", queue_id)
        expected = movement._queue_slot_position.__wrapped__(
            queued, geometry, agent_id, "toilet_01", queue_id
        )
        assert repr(found) == repr(expected)
    point = next(item for item in geometry.service_points if item.queue_id == queue_id)
    found = world_move.facility_approach_route(queued, "A001", "toilet_01", point.id)
    expected = movement.facility_approach_route.__wrapped__(queued, "A001", "toilet_01", point.id)
    assert repr(found) == repr(expected)
    assert world_move.facility_approach_route(queued, "A001", "toilet_01", "none") is None


def _resource_queues(world: WorldState) -> tuple[str, str] | None:
    for queue_id, row in sorted(
        world.model_state[facility_queues.FACILITY_QUEUES_STATE_KEY]["queues"].items()
    ):
        if world.facilities[row["facility_id"]].kind in {"water_station", "meal_distribution"}:
            return row["facility_id"], queue_id
    return None


def test_a_resource_fifo_serves_as_the_python_rule_does(queued: WorldState) -> None:
    found = _resource_queues(queued)
    assert found is not None
    facility_id, queue_id = found
    kind = queued.facilities[facility_id].kind
    resource = "water_ml" if kind == "water_station" else "food_meal_units"
    zone = queued.facilities[facility_id].zone_id
    manager = ResourceServiceManager(queued)
    agent_id = "A001"
    queued.agents[agent_id].zone_id = zone
    queued.model_state["movement_v1"]["positions"][agent_id]["zone_id"] = zone
    kernel = _rules("service")
    # The request.
    ours, theirs = _copy(queued), _copy(queued)
    theirs_receipt = ResourceServiceManager.request.__wrapped__(
        ResourceServiceManager(theirs), agent_id, facility_id, validate_state=False
    )
    our_receipt = kernel.request(
        ResourceServiceManager(ours), agent_id, facility_id, validate_state=False
    )
    assert repr(our_receipt) == repr(theirs_receipt)
    assert _bytes(ours) == _bytes(theirs)
    manager.request(agent_id, facility_id, validate_state=False)
    # Each stop and the start of a service, on a world that walks no further.
    queued.model_state["movement_v1"]["positions"][agent_id]["point"] = queued.model_configs[
        "facility_geometry"
    ]["facility_points"][facility_id]
    method = ResourceServiceManager._serve_independent_head.__wrapped__
    minute = queued.minute_index

    def compare(world: WorldState, quantity: int | None = None) -> Any:
        ours, theirs = _copy(world), _copy(world)
        managers = ResourceServiceManager(ours), ResourceServiceManager(theirs)
        if quantity is not None:
            key = (
                "water_ml_per_service" if resource == "water_ml" else "food_meal_units_per_service"
            )
            for each in managers:
                each.config[key] = quantity
        expected = method(managers[1], facility_id, resource, queue_id, minute)
        result = kernel.serve_independent_head(managers[0], facility_id, resource, queue_id, minute)
        assert tuple(result) == tuple(expected)
        assert _bytes(ours) == _bytes(theirs)
        assert [repr(event) for event in ours.event_queue.pending()] == [
            repr(event) for event in theirs.event_queue.pending()
        ]
        return result

    started = compare(queued)
    assert started[0] is not None or started[1] is None or started[1][1]
    # No staff.
    without = _copy(queued)
    for staff in without.staff.values():
        staff.zone_id = "corridor"
    assert compare(without)[1][1] == "staff"
    # No stock.
    assert compare(queued, quantity=10**12)[1][1] == "stock"
    # The Run's end.
    late = _copy(queued)
    late.minute_index = late.duration_minutes - 1
    minute = late.minute_index
    assert compare(late)[1][1] == "run_end"
    minute = queued.minute_index
    # Its stall records: the head's kept, or the first queue's new.
    stalls = [(agent_id, "staff")]
    previous = {"agent_id": agent_id, "reason": "staff"}
    for wanted, known in (
        (stalls, None),
        (stalls, previous),
        ([], None),
        (stalls, {"agent_id": "A", "reason": "stock"}),
    ):
        ours, theirs = _copy(queued), _copy(queued)
        ResourceServiceManager._record_independent_queue_stall.__wrapped__(
            ResourceServiceManager(theirs), facility_id, list(wanted), known
        )
        kernel.record_independent_stall(
            ResourceServiceManager(ours), facility_id, list(wanted), known
        )
        assert _bytes(ours) == _bytes(theirs)


def test_a_completed_service_leaves_its_fifo_as_the_python_rule_does(queued: WorldState) -> None:
    found = _resource_queues(queued)
    assert found is not None
    facility_id, _ = found
    zone = queued.facilities[facility_id].zone_id
    for agent_id in ("A001", "A002"):
        queued.agents[agent_id].zone_id = zone
        position = queued.model_state["movement_v1"]["positions"][agent_id]
        position["zone_id"] = zone
    manager = ResourceServiceManager(queued)
    manager.request("A001", facility_id, validate_state=False)
    for staff in queued.staff.values():
        staff.zone_id = zone
    geometry = queued.model_configs["facility_geometry"]
    queue_state = queued.model_state[facility_queues.FACILITY_QUEUES_STATE_KEY]
    request = next(iter(queue_state["requests"].values()))
    point = next(
        item for item in geometry["service_points"] if item["queue_id"] == request["queue_id"]
    )
    queued.model_state["movement_v1"]["positions"]["A001"]["point"] = point["point_m"]
    for _ in range(30):
        manager.arbitrate(queued.minute_index, validate_state=False)
        pending = [
            event
            for event in queued.event_queue.pending()
            if event.event_type == "resource_service_complete"
        ]
        if pending:
            break
        queued.minute_index += 1
    assert pending, "the service never started"
    event = pending[0]
    queued.minute_index = event.due_minute
    ours, theirs = _copy(queued), _copy(queued)
    ResourceServiceManager.complete.__wrapped__(ResourceServiceManager(theirs), event)
    _rules("service").complete(ResourceServiceManager(ours), event)
    assert _bytes(ours) == _bytes(theirs)
    assert not ours.model_state[facility_queues.FACILITY_QUEUES_STATE_KEY]["requests"]


def _toilet(world: WorldState) -> ToiletServiceManager:
    return ToiletServiceManager(world, min_service_minutes=2, max_service_minutes=4)


def test_a_toilet_fifo_is_the_python_rule_s(queued: WorldState) -> None:
    from sheltersim.core.rng import CounterRNG

    kernel = _rules("toilet")
    queue = "toilet_01-left-fifo"
    zone = queued.facilities["toilet_01"].zone_id
    for agent_id in ("A001", "A002"):
        queued.agents[agent_id].zone_id = zone
        queued.model_state["movement_v1"]["positions"][agent_id]["zone_id"] = zone

    def same_request(world: WorldState, agent_id: str, **options: Any) -> None:
        ours, theirs = _copy(world), _copy(world)
        expected = ToiletServiceManager.request.__wrapped__(
            _toilet(theirs), agent_id, "toilet_01", validate_state=False, **options
        )
        result = kernel.request(
            _toilet(ours), agent_id, "toilet_01", validate_state=False, **options
        )
        assert repr(result) == repr(expected)
        assert _bytes(ours) == _bytes(theirs)

    same_request(queued, "A001", queue_id=queue)
    same_request(queued, "A001")
    _toilet(queued).request("A001", "toilet_01", queue_id=queue, validate_state=False)
    _toilet(queued).request("A002", "toilet_01", queue_id=queue, validate_state=False)
    method = ToiletServiceManager._serve_independent_head.__wrapped__
    rng = CounterRNG(queued.seed)

    def same_head(world: WorldState, minute: int) -> Any:
        ours, theirs = _copy(world), _copy(world)
        expected = method(_toilet(theirs), "toilet_01", queue, minute, rng)
        result = kernel.serve_independent_head(_toilet(ours), "toilet_01", queue, minute, rng)
        assert result == expected
        assert _bytes(ours) == _bytes(theirs)
        return ours, result

    # Each stop: the walk, a point with no place, the Run's end, no skilled staff.
    same_head(queued, queued.minute_index)
    point = next(
        item
        for item in queued.model_configs["facility_geometry"]["service_points"]
        if item["queue_id"] == queue
    )
    queued.model_state["movement_v1"]["positions"]["A001"]["point"] = dict(point["point_m"])
    full = _copy(queued)
    full.facilities["toilet_01"].working_units = 0
    assert same_head(full, full.minute_index)[1] is None
    late = _copy(queued)
    late.minute_index = late.duration_minutes - 1
    assert same_head(late, late.minute_index)[1] is None
    skilled = _copy(queued)
    for item in skilled.model_configs["facility_geometry"]["service_points"]:
        if item["queue_id"] == queue:
            item["access"] = {
                "accessibility": "accessible",
                "required_access_tags": [],
                "required_staff_skills": ["no_such_skill"],
            }
    assert same_head(skilled, skilled.minute_index)[1] is None
    # With skilled staff here the service starts and holds the person.
    staffed = _copy(skilled)
    member = next(iter(staffed.staff.values()))
    member.zone_id = zone
    member.skills = [*member.skills, "no_such_skill"]
    served, started = same_head(staffed, staffed.minute_index)
    assert started is not None
    # And the completion lets them go.
    event = served.event_queue.find(started)
    assert event.payload["staff_id"] == member.id
    served.minute_index = event.due_minute
    from sheltersim.events.handlers import ToiletServiceCompletionHandler

    ours, theirs = _copy(served), _copy(served)
    handler = ToiletServiceCompletionHandler()
    ToiletServiceCompletionHandler.apply.__wrapped__(handler, theirs, event)
    kernel.complete(handler, ours, event)
    assert _bytes(ours) == _bytes(theirs)
    assert ours.staff[member.id].active_task_id is None


# ---- Next to a coefficient or a limit ------------------------------------------------------------


def _new_toilet(world: WorldState, facility_id: str = "extra") -> str:
    from sheltersim.domain.models import FacilityState

    world.facilities[facility_id] = FacilityState(
        id=facility_id, kind="toilet", zone_id="toilet", capacity=1, working_units=1
    )
    return facility_id


def _same_point(world: WorldState, facility_id: str) -> Any:
    point = _rules().installed_facility_point(world, facility_id)
    assert point == geometry_overlay.installed_facility_point.__wrapped__(world, facility_id)
    return point


@pytest.mark.parametrize("distance", [0.5999, 0.6, 0.6001, 0.603])
def test_the_separation_of_a_placed_facility_is_the_python_rule_s(
    world: WorldState, distance: float
) -> None:
    from sheltersim.contracts.extensions import Point2D
    from sheltersim.region.facility import facility_grid_point

    facility_id = _new_toilet(world)
    geometry = world.model_configs["facility_geometry"]
    zone = next(item for item in geometry["zones"] if item["zone_id"] == "toilet")
    polygon = tuple(Point2D.model_validate(point) for point in zone["polygon"])
    others = [
        key
        for key, item in world.facilities.items()
        if key != facility_id and item.zone_id == "toilet" and key in geometry["facility_points"]
    ]
    slot = facility_grid_point(polygon, 0, len(others) + 1)
    # Everyone else this far from the first slot, on the side away from the zone's middle.
    for key in others:
        geometry["facility_points"][key] = {"x": slot.x, "y": slot.y + distance}
    point = _same_point(world, facility_id)
    # 0.6 itself is what the floats make of it, and only the comparison above speaks for it.
    if distance != 0.6:
        assert (point == slot) is (distance > 0.6)


def test_the_search_of_a_grid_ends_where_the_python_rule_s_does(world: WorldState) -> None:
    from sheltersim.contracts.extensions import Point2D
    from sheltersim.region.facility import facility_grid_point

    facility_id = _new_toilet(world)
    geometry = world.model_configs["facility_geometry"]
    zone = next(item for item in geometry["zones"] if item["zone_id"] == "toilet")
    polygon = tuple(Point2D.model_validate(point) for point in zone["polygon"])
    others = sum(
        key != facility_id and item.zone_id == "toilet" and key in geometry["facility_points"]
        for key, item in world.facilities.items()
    )
    xs = [point.x for point in polygon]
    ys = [point.y for point in polygon]
    state = world.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]
    # Free only near a slot of the given grid: each room blocks one side of it.
    for count, found in ((others + 16, True), (others + 17, False)):
        slot = facility_grid_point(polygon, count - 1, count)
        gap = 0.001
        sides = (
            (min(xs) - 1, min(ys) - 1, slot.x - gap, max(ys) + 1),
            (slot.x + gap, min(ys) - 1, max(xs) + 1, max(ys) + 1),
            (slot.x - gap, min(ys) - 1, slot.x + gap, slot.y - gap),
            (slot.x - gap, slot.y + gap, slot.x + gap, max(ys) + 1),
        )
        state["temporary_rooms"].clear()
        for index, (left, top, right, bottom) in enumerate(sides):
            state["temporary_rooms"][f"block{index}"] = {
                "active": True,
                "polygon_m": [
                    {"x": left, "y": top},
                    {"x": right, "y": top},
                    {"x": right, "y": bottom},
                    {"x": left, "y": bottom},
                ],
            }
        point = _same_point(world, facility_id)
        assert (point is not None) is found


def test_a_wall_s_clearance_is_the_python_rule_s(world: WorldState) -> None:
    from sheltersim.contracts.spatial import SpatialConstraints

    toilet = next(key for key, item in world.facilities.items() if item.kind == "toilet")
    zone_id = world.facilities[toilet].zone_id
    raw = world.model_configs["spatial_constraints"]
    walls = SpatialConstraints.model_validate(raw).walls
    rule = geometry_overlay.validate_geometry_change.__wrapped__
    kernel = _rules().validate_geometry_change
    accepted = 0
    for wall in walls:
        dx, dy = wall.end.x - wall.start.x, wall.end.y - wall.start.y
        length = (dx * dx + dy * dy) ** 0.5
        if length == 0:
            continue
        nx, ny = -dy / length, dx / length
        for fraction in (0.25, 0.5, 0.75):
            for side in (1, -1):
                for clearance in (0.499, 0.503):
                    point = {
                        "x": wall.start.x + dx * fraction + nx * side * clearance,
                        "y": wall.start.y + dy * fraction + ny * side * clearance,
                    }
                    parameters = {"point_m": point}
                    try:
                        rule(world, "move_facility", toilet, parameters, None)
                    except ValueError:
                        with pytest.raises(native.Unsupported):
                            kernel(world, "move_facility", toilet, parameters, None)
                        continue
                    kernel(world, "move_facility", toilet, parameters, None)
                    accepted += clearance == 0.503
    assert accepted, f"no point of {zone_id} beside a wall to compare"


def test_a_full_overlay_history_drops_its_oldest_record(world: WorldState) -> None:
    state = world.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]
    state["history"].extend(
        {
            "transition_id": f"old{index}",
            "op": "set_partition",
            "target_id": ROOM_ZONE,
            "status": "applied",
            "causation_id": None,
            "event_id": f"old{index}",
            "source_ids": [],
            "minute": 0,
        }
        for index in range(20_000)
    )
    event = _event(world, "set_partition", ROOM_ZONE, {"polygon_m": _corner(world, ROOM_ZONE)}, 50)
    _same(
        world,
        _rules().apply_geometry_change,
        geometry_overlay.apply_geometry_change.__wrapped__,
        event,
    )


@pytest.mark.parametrize(("blocked", "found"), [(255, True), (256, False)])
def test_a_bed_s_routes_end_where_the_python_rule_s_do(
    world: WorldState, blocked: int, found: bool
) -> None:
    room = _room_record(world, "room", people=400, beds=1)
    free = room["bed_points_m"][0]
    corner = dict(room["polygon_m"][0])
    corner = {"x": corner["x"] + 0.2, "y": corner["y"] + 0.2}
    # Beds no route reaches, each one try, ahead of the bed that has a route.
    room["bed_points_m"] = [corner] * blocked + [free]
    room["sleeping_capacity"] = len(room["bed_points_m"])
    world.model_state[geometry_overlay.GEOMETRY_OVERLAY_KEY]["temporary_rooms"]["room"] = room
    world.model_configs["spatial_constraints"].setdefault("obstacles", []).append(
        {
            "id": "around-the-corner",
            "polygon": [
                {"x": corner["x"] - 0.05, "y": corner["y"] - 0.05},
                {"x": corner["x"] + 0.05, "y": corner["y"] - 0.05},
                {"x": corner["x"] + 0.05, "y": corner["y"] + 0.05},
                {"x": corner["x"] - 0.05, "y": corner["y"] + 0.05},
            ],
            "source_ids": [],
        }
    )
    agent_id = _person_in_room_zone(world)
    rule = geometry_overlay.reserve_sleep_bed.__wrapped__
    kernel = _rules().reserve_sleep_bed
    ours, theirs = deepcopy(world), deepcopy(world)
    assert json.dumps(_plain(kernel(ours, agent_id))) == json.dumps(_plain(rule(theirs, agent_id)))
    assert (rule(deepcopy(world), agent_id) is not None) is found
