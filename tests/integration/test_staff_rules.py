"""staff_operations_v1 and shared_operations_v1 owned by Rust, with the rules of a tick.

native/src/rules/staff.rs and common_operations.rs move the rules of a tick where no staff member
starts anything, and a care receipt; the Python rules stay as the comparison base and give every
error text (docs/rust-port/lanes/m4-staff.md). The rules are compared on the gate by
benchmarks/rule_shadow; here, on small worlds and on the values no scenario reaches.
"""

from __future__ import annotations

import copy
import json
import random
from typing import Any

import pytest
from support.paths import SMALL_REPLAY

from sheltersim import native
from sheltersim.contracts.operations import SHARED_OPERATIONS_KEY, NumericStateChange
from sheltersim.core.state_mirror import rust_validated
from sheltersim.core.world import WorldState
from sheltersim.domain.care_receipts import make_care_transition_record
from sheltersim.domain.common_operations import (
    _TICK_PASSED,
    _TICK_VALID,
    record_shared_operation,
    validate_shared_operations_state,
)
from sheltersim.domain.models import (
    Activity,
    SupportRequestState,
    SupportRequestStatus,
)
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.domain.staff import _CARE_SKILL_ALTERNATIVES, StaffService
from sheltersim.domain.staff_operations import (
    STAFF_OPERATIONS_STATE_KEY,
    assign_staff_operations,
    schedule_staff_operation_relief,
    update_staff_operations,
    validate_staff_operations_state,
)
from sheltersim.domain.zones import ZoneGraph
from sheltersim.experiments import metrics
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.space import SpaceRouter

needs_kernels = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")
pytestmark = needs_kernels

KEY = STAFF_OPERATIONS_STATE_KEY
SHARED = SHARED_OPERATIONS_KEY
KINDS = (*_CARE_SKILL_ALTERNATIVES, "a_kind_of_no_table")


def _world(
    *, skills: tuple[str, ...] = ("support",), configs: dict[str, Any] | None = None
) -> WorldState:
    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = 200
    document["scheduled_events"] = []
    document["model_configs"]["staff_operations"] = {}
    document["model_configs"]["shared_operations"] = {
        "schema_version": 1,
        "policy_version": "shared-operations-v1",
    }
    document["model_configs"].update(configs or {})
    template = document["staff"][0]
    document["staff"] = [
        {**template, "id": f"S{index:02}", "skills": list(skills)} for index in range(1, 4)
    ]
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    initialize_simulator_extensions(world)
    return world


def _twins(**options: Any) -> tuple[WorldState, WorldState]:
    return _world(**options), _world(**options)


def _text(value: Any) -> str:
    """Float bits, 1 against 1.0, and key order all show."""
    return json.dumps(value)


def _mirror(world: WorldState, key: str = KEY) -> Any:
    return world._core.mirror(key)


def _zones(world: WorldState) -> list[str]:
    return sorted(world.zones)


# ---- The rows ----


def test_the_rows_write_the_bytes_of_their_trees() -> None:
    world = _world()
    for key in (KEY, SHARED):
        assert _mirror(world, key).has_row
        assert _mirror(world, key).row_json() == native.canonical_json(world.model_state[key])


def test_a_forwarded_task_reaches_the_row_and_a_finished_task_too() -> None:
    world = _world()
    state = world.model_state[KEY]
    record = dict(state["staff"]["S01"])
    state["staff"]["S01"] = {**record, "phase": "travel", "task_id": "t", "task_kind": "patrol"}
    state["staff"]["S01"]["phase"] = "working"
    state["staff"]["S01"]["route_edge_ids"].append("e")
    assert _mirror(world).verify()
    state["history"].append(
        {
            "task_id": "t",
            "staff_id": "S01",
            "task_kind": "patrol",
            "target_id": None,
            "target_zone_id": _zones(world)[0],
            "reason": "r",
            "status": "completed",
            "start_minute": 0,
            "arrival_minute": 0,
            "complete_minute": 1,
            "travel_minutes": 0,
            "causation_event_ids": ["e"],
        }
    )
    assert _mirror(world).verify()


def test_a_value_the_row_cant_hold_taints_it_and_the_python_rule_still_runs() -> None:
    world, plain = _twins()
    for each in (world, plain):
        zone = _zones(each)[0]
        each.model_state[KEY]["zones"][zone]["sanitation"] = 1
    assert not _mirror(world).has_row
    with pytest.raises(native.Unsupported):
        native.rules("staff").update_operations(world, 5)
    update_staff_operations(world, 5)
    update_staff_operations.__wrapped__(plain, 5)
    assert _text(world.model_state[KEY]) == _text(plain.model_state[KEY])


# ---- update_staff_operations ----


def test_the_sanitation_is_the_python_rules_on_random_rates() -> None:
    generator = random.Random(7)
    for _round in range(40):
        rate = generator.uniform(0.0, 1.0) ** 3
        zone_rate = generator.uniform(0.0, 0.01)
        world, plain = _twins()
        zone = generator.choice(_zones(world))
        for each in (world, plain):
            config = each.model_state[KEY]["config"]
            config["sanitation_use_per_person_minute"] = rate
            config["effects_by_zone"] = {zone: {"sanitation_use_per_person_minute": zone_rate}}
        minute = 0
        for _step in range(4):
            minute += generator.randrange(0, 90)
            kernel_before = dict(_mirror(world).stats())
            native.rules("staff").update_operations(world, minute)
            assert _mirror(world).stats()["whole_writes"] == kernel_before["whole_writes"]
            update_staff_operations.__wrapped__(plain, minute)
            assert _text(world.model_state[KEY]) == _text(plain.model_state[KEY])
        assert _mirror(world).verify()


def test_a_rate_of_one_percent_more_shows() -> None:
    world, other = _twins()
    other.model_state[KEY]["config"]["sanitation_use_per_person_minute"] = 0.000005 * 1.01
    update_staff_operations(world, 100)
    update_staff_operations(other, 100)
    assert _text(world.model_state[KEY]["zones"]) != _text(other.model_state[KEY]["zones"])


@pytest.mark.parametrize(
    "spoil",
    [
        lambda config, _zone: config.__setitem__("sanitation_use_per_person_minute", 0),
        lambda config, _zone: config.pop("effects_by_zone"),
        lambda config, zone: config.__setitem__(
            "effects_by_zone", {zone: {"sanitation_use_per_person_minute": 0}}
        ),
    ],
)
def test_what_the_update_doesnt_decide_changes_nothing_and_goes_to_the_python_rule(
    spoil: Any,
) -> None:
    world, plain = _twins()
    zone = _zones(world)[0]
    for each in (world, plain):
        spoil(each.model_state[KEY]["config"], zone)
    before = _text(world.model_state[KEY])
    with pytest.raises(native.Unsupported):
        native.rules("staff").update_operations(world, 50)
    assert _text(world.model_state[KEY]) == before
    outcomes = []
    for rule, each in (
        (update_staff_operations, world),
        (update_staff_operations.__wrapped__, plain),
    ):
        try:
            outcomes.append(rule(each, 50))
        except KeyError as error:
            outcomes.append(repr(error))
    assert outcomes[0] == outcomes[1]
    assert _text(world.model_state[KEY]) == _text(plain.model_state[KEY])


# ---- assign_staff_operations, schedule_staff_operation_relief ----


def _set_stress(world: WorldState, zone: str, stress: float) -> None:
    for agent in world.agents.values():
        agent.zone_id = zone
        agent.stress = stress


@pytest.mark.parametrize(
    ("skills", "name", "threshold", "above_is_a_task"),
    [(("patrol",), "patrol_stress_at_least", 0.6, True)],
)
def test_a_free_worker_with_a_task_goes_to_the_python_rule(
    skills: tuple[str, ...], name: str, threshold: float, above_is_a_task: bool
) -> None:
    world = _world(skills=skills)
    zone = _zones(world)[0]
    router = object()
    kernel = native.rules("staff").assign_operations
    _set_stress(world, zone, threshold)
    with pytest.raises(native.Unsupported):
        kernel(world, 1, router)
    _set_stress(world, zone, 0.5999999)
    assert kernel(world, 1, router) == ()
    # A zone's own threshold replaces it, as an int too.
    world.model_state[KEY]["config"]["thresholds_by_zone"] = {zone: {name: 1}}
    _set_stress(world, zone, 0.9)
    assert kernel(world, 1, router) == ()
    _set_stress(world, zone, 1.0)
    with pytest.raises(native.Unsupported):
        kernel(world, 1, router)


def test_the_cleaning_threshold_is_the_rules() -> None:
    world = _world(skills=("cleaning",))
    zone = _zones(world)[0]
    kernel = native.rules("staff").assign_operations
    state = world.model_state[KEY]
    state["zones"][zone]["sanitation"] = 0.85
    assert kernel(world, 1, object()) == ()
    state["zones"][zone]["sanitation"] = 0.8499999
    with pytest.raises(native.Unsupported):
        kernel(world, 1, object())
    # The zone is taken by a worker on it: S02 stays free and has nothing to do.
    state["staff"]["S01"] = {
        **state["staff"]["S01"],
        "phase": "working",
        "task_id": "t",
        "task_kind": "cleaning",
        "target_zone_id": zone,
        "reason": "r",
        "started_minute": 0,
        "arrival_minute": 0,
        "due_minute": 9,
    }
    assert kernel(world, 1, object()) == ()


def test_the_outreach_threshold_is_the_rules() -> None:
    from sheltersim.domain.body_state import canonical_body_config

    world = _world(
        skills=("information",),
        configs={
            "body_dynamics": canonical_body_config(None),
            "policy_registry": {"schema_version": 2, "psychology": "life-state-feedback-v1"},
        },
    )
    zone = _zones(world)[0]
    kernel = native.rules("staff").assign_operations
    body = world.model_state["body_state_v1"]["agents"]
    for agent in world.agents.values():
        agent.zone_id = zone
    for record in body.values():
        record["uncertainty"] = 0.45
    with pytest.raises(native.Unsupported):
        kernel(world, 1, object())
    for record in body.values():
        record["uncertainty"] = 0.4499999
    assert kernel(world, 1, object()) == ()


def test_a_repair_goes_to_the_python_rule_and_so_does_another_formula() -> None:
    world = _world(skills=("maintenance",))
    kernel = native.rules("staff").assign_operations
    assert kernel(world, 1, object()) == ()
    facility = next(iter(world.facilities.values()))
    facility.working_units = facility.capacity - 1
    with pytest.raises(native.Unsupported):
        kernel(world, 1, object())
    facility.working_units = facility.capacity
    world.model_configs["policy_registry"] = {"staff_operations": "a-later-formula"}
    with pytest.raises(native.Unsupported):
        kernel(world, 1, object())


@pytest.mark.parametrize("skills", [("support",), ("patrol",), ("cleaning",), ("repair",), ("x",)])
def test_a_tick_with_nothing_to_start_changes_nothing_as_in_the_python_rule(
    skills: tuple[str, ...],
) -> None:
    world = _world(skills=skills)
    before = world.state_digest()
    assert native.rules("staff").relief(world, 3) == ()
    assert schedule_staff_operation_relief.__wrapped__(world, 3) == ()
    assert native.rules("staff").assign_operations(world, 3, object()) == ()
    assert world.state_digest() == before
    graph = ZoneGraph.from_states(world.zones, world.zone_edges)
    assert assign_staff_operations.__wrapped__(world, 3, SpaceRouter(graph)) == ()
    assert world.state_digest() == before


def test_a_task_under_way_sends_the_relief_to_the_python_rule() -> None:
    world = _world()
    state = world.model_state[KEY]
    state["staff"]["S01"] = {**state["staff"]["S01"], "phase": "working"}
    with pytest.raises(native.Unsupported):
        native.rules("staff").relief(world, 3)
    assert native.rules("staff").relief(world, world.duration_minutes - 1) == ()


# ---- The validator ----


def test_the_validator_accepts_a_good_state_and_nothing_python_turns_down() -> None:
    world = _world()
    assert rust_validated(world, KEY)
    zone = _zones(world)[0]
    spoils = [
        lambda state: state["zones"][zone].__setitem__("sanitation", 1.5),
        lambda state: state["zones"][zone].__setitem__("updated_minute", -1),
        lambda state: state.__setitem__("schema_version", 2),
        lambda state: state.__setitem__("policy_version", "other"),
        lambda state: state.__setitem__("source_id", "other"),
        lambda state: state["zones"].pop(zone),
        lambda state: state["staff"].pop("S01"),
        lambda state: state["staff"]["S01"].__setitem__("reason", "kept"),
        lambda state: state["staff"]["S01"].__setitem__("travel_minutes", 3),
        lambda state: state["staff"]["S01"]["causation_event_ids"].append("e"),
        lambda state: state["config"].pop("cleaning_sanitation_recovery"),
        lambda state: state["config"].__setitem__("patrol_reassurance", 1.5),
        lambda state: state["config"].__setitem__("patrol_reassurance", True),
        lambda state: state["config"].__setitem__("patrol_reassurance", float("nan")),
        lambda state: state["config"].__setitem__("an_unknown_key", 1),
        lambda state: state["config"]["task_duration_minutes"].__setitem__("patrol", 0),
        lambda state: state["config"]["task_duration_minutes"].__setitem__("patrol", 15.0),
        lambda state: state["config"]["task_duration_minutes"].pop("patrol"),
        lambda state: state["config"].__setitem__(
            "thresholds_by_zone", {"nowhere": {"patrol_stress_at_least": 0.5}}
        ),
        lambda state: state["config"].__setitem__("thresholds_by_zone", {zone: {}}),
        lambda state: state["config"].__setitem__(
            "thresholds_by_zone", {zone: {"patrol_stress_at_least": 2}}
        ),
        lambda state: state["config"].__setitem__(
            "effects_by_zone", {zone: {"sanitation_use_per_person_minute": 0.02}}
        ),
        lambda state: state["history"].append(_finished(zone, staff_id="nobody")),
        lambda state: state["history"].append(_finished(zone, task_kind="other")),
        lambda state: state["history"].append(_finished(zone, status="other")),
        lambda state: state["history"].append(_finished(zone, target_zone_id="nowhere")),
        lambda state: state["history"].append(_finished(zone, target_id="nothing")),
        lambda state: state["history"].append(_finished(zone, start_minute=-1)),
        lambda state: state["history"].append(_finished(zone, complete_minute=1, start_minute=2)),
        lambda state: state["history"].append(_finished(zone, arrival_minute=None)),
        lambda state: state["history"].append(_finished(zone, arrival_minute=9)),
        lambda state: state["history"].append(_finished(zone, causation_event_ids=[""])),
    ]
    for spoil in spoils:
        spoiled = _world()
        spoil(spoiled.model_state[KEY])
        with pytest.raises((ValueError, KeyError, TypeError)):
            validate_staff_operations_state(spoiled)
        assert not rust_validated(spoiled, KEY)


def _finished(zone: str, **fields: Any) -> dict[str, Any]:
    return {
        "task_id": "t",
        "staff_id": "S01",
        "task_kind": "patrol",
        "target_id": None,
        "target_zone_id": zone,
        "reason": "r",
        "status": "completed",
        "start_minute": 0,
        "arrival_minute": 0,
        "complete_minute": 1,
        "travel_minutes": 0,
        "causation_event_ids": ["e"],
        **fields,
    }


def test_the_validator_takes_good_finished_tasks_an_older_config_and_leaves_a_task_to_python() -> (
    None
):
    world = _world()
    zone = _zones(world)[0]
    state = world.model_state[KEY]
    state["history"].append(_finished(zone))
    state["history"].append(
        _finished(zone, status="interrupted", arrival_minute=None, travel_minutes=2, reason="")
    )
    validate_staff_operations_state(world)
    assert rust_validated(world, KEY)
    del state["config"]["thresholds_by_zone"]
    del state["config"]["effects_by_zone"]
    validate_staff_operations_state(world)
    assert rust_validated(world, KEY)
    state["staff"]["S01"] = {**state["staff"]["S01"], "phase": "working"}
    assert not rust_validated(world, KEY)


# ---- StaffService.assign_pending ----


def _request(world: WorldState, number: int, agent_id: str, minute: int = 0) -> SupportRequestState:
    request = SupportRequestState(
        id=f"support-{number}",
        agent_id=agent_id,
        requested_minute=minute,
        timeout_minute=world.duration_minutes - 1,
    )
    world.support_requests[request.id] = request
    return request


def _care(world: WorldState, request: SupportRequestState, kind: str) -> None:
    world.model_state[SHARED]["support_requests"][request.id] = {
        "request_id": request.id,
        "agent_id": request.agent_id,
        "household_id": None,
        "support_kind": kind,
        "status": "requested",
        "created_minute": request.requested_minute,
        "updated_minute": request.requested_minute,
        "latest_event_id": f"care-{request.id}",
    }


def _waiting_fields(world: WorldState) -> list[Any]:
    return [
        (
            request.id,
            request.last_assignment_attempt_minute,
            request.last_assignment_wait_reason_code,
        )
        for request in world.support_requests.values()
    ]


def _busy(world: WorldState, staff_id: str) -> None:
    world.staff[staff_id].active_task_id = f"task-{staff_id}"


def test_requests_that_stay_pending_are_the_python_rules() -> None:
    generator = random.Random(11)
    for _round in range(30):
        world, plain = _twins(skills=generator.choice([("support",), ("nursing",), ("hygiene",)]))
        minute = 0
        people = sorted(world.agents)
        kinds = [generator.choice(KINDS[:-1]) for _ in people]
        for each in (world, plain):
            each.minute_index = minute
            for number, (agent_id, kind) in enumerate(zip(people, kinds)):
                request = _request(each, number, agent_id)
                if number % 2:
                    _care(each, request, kind)
            for staff_id in each.staff:
                _busy(each, staff_id)
        service = StaffService(world)
        twin = StaffService(plain)
        ran = 0
        try:
            first = native.rules("staff").assign_pending(service, minute, validate_state=False)
            ran = 1
        except native.Unsupported:
            first = service.assign_pending(minute, validate_state=False)
        second = StaffService.assign_pending.__wrapped__(twin, minute, validate_state=False)
        assert first == second == ()
        assert ran == 1
        assert _waiting_fields(world) == _waiting_fields(plain)
        assert _text(dict(world.metric_state)) == _text(dict(plain.metric_state))
        # A second call at the same minute adds nothing, as in the rule.
        native.rules("staff").assign_pending(service, minute, validate_state=False)
        assert _text(dict(world.metric_state)) == _text(dict(plain.metric_state))


def test_the_wait_counts_staff_off_shift_away_and_busy_as_the_python_rule_does() -> None:
    windows = {"S01": [{"start_minute": 100, "end_minute": 110}]}
    configs = {"staff_dynamics": {"shift_windows_by_staff": windows}}
    world, plain = _twins(configs=configs)
    for each in (world, plain):
        for staff_id in ("S02", "S03"):
            _busy(each, staff_id)
        _request(each, 1, sorted(each.agents)[0])
    assert native.rules("staff").assign_pending(StaffService(world), 0, validate_state=False) == ()
    StaffService.assign_pending.__wrapped__(StaffService(plain), 0, validate_state=False)
    assert _text(dict(world.metric_state)) == _text(dict(plain.metric_state))
    assert world.metric_state["support_wait_staff_off_shift_request_minutes"] == 1.0
    assert world.metric_state["support_wait_staff_busy_request_minutes"] == 1.0
    assert world.metric_state["support_no_qualified_worker_request_minutes"] == 0.0


def test_a_free_worker_goes_past_the_plain_kernel() -> None:
    world = _world()
    _request(world, 1, sorted(world.agents)[0])
    service = StaffService(world)
    before = (_waiting_fields(world), _text(dict(world.metric_state)))
    with pytest.raises(native.Unsupported):
        native.rules("staff").assign_none(service, 0, validate_state=False)
    assert (_waiting_fields(world), _text(dict(world.metric_state))) == before
    # No route is known here, so it is assigned where it stands.
    assert service.assign_pending(0, validate_state=False) == ("support-1",)


def test_what_the_assignment_doesnt_decide_goes_to_the_python_rule() -> None:
    world = _world()
    for staff_id in world.staff:
        _busy(world, staff_id)
    request = _request(world, 1, sorted(world.agents)[0])
    service = StaffService(world)
    kernel = native.rules("staff").assign_none
    with pytest.raises(native.Unsupported):
        kernel(service, 0)  # It validates the state.
    with pytest.raises(native.Unsupported):
        kernel(service, 1, validate_state=False)  # Not the world's minute.
    world.model_state["llm"] = {"support_priorities": {request.id: 1}}
    with pytest.raises(native.Unsupported):
        kernel(service, 0, validate_state=False)
    world.model_state["llm"] = {"support_priorities": {request.id: True}}
    assert kernel(service, 0, validate_state=False) == ()
    world.metric_state["support_assignment_metrics_minute"] = -1.0
    request.report_writes_to([])
    with pytest.raises(native.Unsupported):
        kernel(service, 0, validate_state=False)


# ---- What the metrics read ----


def _both_snapshots(world: WorldState, minute: int) -> str:
    active = sorted(
        (
            request
            for request in world.support_requests.values()
            if request.status in (SupportRequestStatus.PENDING, SupportRequestStatus.ASSIGNED)
        ),
        key=lambda request: request.id,
    )
    first = native.rules("staff").wait_snapshot(world, active, minute)
    second = metrics._support_wait_snapshot.__wrapped__(world, active, minute)
    assert _text(first) == _text(second)
    return _text(first)


@pytest.mark.parametrize("kind", KINDS)
def test_each_kind_asks_for_the_skills_of_the_table(kind: str) -> None:
    required = _CARE_SKILL_ALTERNATIVES.get(kind, {"support"})
    every_skill = sorted(
        {skill for skills in _CARE_SKILL_ALTERNATIVES.values() for skill in skills}
    )
    for skill in [*every_skill, "other"]:
        world = _world(skills=(skill,))
        for staff_id in world.staff:
            _busy(world, staff_id)
        request = _request(world, 1, sorted(world.agents)[0])
        if kind != "emotional_support":
            # A kind no contract takes reads the same; only the row of the head would refuse it.
            world.model_state[SHARED]["support_requests"][request.id] = {
                "support_kind": kind,
            }
        answer = json.loads(_both_snapshots(world, 0))
        assert (answer[1] == []) == (skill in required)


def test_the_reasons_are_the_python_rules_on_random_staff() -> None:
    generator = random.Random(23)
    reasons_seen: set[str] = set()
    for _round in range(120):
        windows = {
            "S01": [{"start_minute": 0, "end_minute": generator.choice([1, 60])}],
        }
        world = _world(
            skills=generator.choice([("support",), ("nursing", "support"), ("privacy",)]),
            configs={"staff_dynamics": {"shift_windows_by_staff": windows}},
        )
        minute = generator.randrange(0, 90)
        world.minute_index = minute
        operations = world.model_state[KEY]["staff"]
        dynamics = world.model_state["staff_dynamics_v1"]["staff"]
        for staff_id in world.staff:
            choice = generator.randrange(5)
            # Every worker but those of the last branches is busy, so no route is read.
            _busy(world, staff_id)
            if choice == 1:
                operations[staff_id] = {**operations[staff_id], "phase": "working"}
            elif choice == 2:
                dynamics[staff_id]["rest_until_minute"] = minute + 5
            elif choice == 3:
                dynamics[staff_id]["rest_until_minute"] = minute
            elif choice == 4:
                world.staff[staff_id].active_task_id = None
                dynamics[staff_id]["rest_until_minute"] = minute + 1
        people = sorted(world.agents)
        for number, agent_id in enumerate(people[:6]):
            request = _request(world, number, agent_id, generator.randrange(0, minute + 1))
            if number % 2:
                _care(world, request, generator.choice(KINDS[:-1]))
            if number % 3 == 2:
                request.status = SupportRequestStatus.ASSIGNED
                request.task_id = f"task-{number}"
                world.agents[agent_id].activity = generator.choice(
                    [Activity.MOVING, Activity.SEEKING_HELP]
                )
        answer = json.loads(_both_snapshots(world, minute))
        for detail in answer[0].values():
            reasons_seen.update(detail["reason_codes"])
    assert {
        "staff_busy",
        "staff_on_other_operation",
        "staff_resting",
        "resident_travel",
        "support_in_progress",
    } <= reasons_seen


def test_a_free_worker_sends_the_snapshot_to_the_python_rule() -> None:
    world = _world()
    request = _request(world, 1, sorted(world.agents)[0])
    with pytest.raises(native.Unsupported):
        native.rules("staff").wait_snapshot(world, [request], 0)


# ---- record_shared_operation ----


def _receipt(world: WorldState, number: int, status: str, **fields: Any) -> Any:
    event_record, receipt = make_care_transition_record(
        world,
        request_id=f"request-{number}",
        agent_id=sorted(world.agents)[0],
        household_id=None,
        support_kind="emotional_support",
        status=status,
        **fields,
    )
    world.event_log.append(event_record)
    return receipt


def _python_twin(world: WorldState) -> Any:
    class Twin:
        def __init__(self) -> None:
            self.model_state = dict(world.model_state)
            self.model_state[SHARED] = copy.deepcopy(
                json.loads(json.dumps(world.model_state[SHARED]))
            )

        def __getattr__(self, name: str) -> Any:
            return getattr(world, name)

    return Twin()


def test_a_request_s_receipts_are_the_python_rules_and_kept_in_the_row() -> None:
    world = _world()
    stress_change = NumericStateChange(
        entity_id=sorted(world.agents)[0],
        entity_kind="agent",
        field_path="stress",
        before=0.5,
        after=world.agents[sorted(world.agents)[0]].stress,
        unit="index",
    )
    kernel = native.rules("common_operations").record_shared_operation
    for status, fields in (
        ("requested", {}),
        ("assigned", {"staff_id": "S01"}),
        ("started", {"staff_id": "S01"}),
        (
            "completed",
            {
                "staff_id": "S01",
                "outcome_observation": "observed_change",
                "state_changes": (stress_change,),
            },
        ),
    ):
        world.minute_index += 1
        receipt = _receipt(world, 1, status, **fields)
        twin = _python_twin(world)
        kernel(world, receipt)
        record_shared_operation.__wrapped__(twin, receipt)
        assert _text(world.model_state[SHARED]) == _text(twin.model_state[SHARED])
        assert _mirror(world, SHARED).verify()
    assert world.model_state[SHARED]["support_requests"] == {}
    assert len(world.model_state[SHARED]["history"]) == 4


@pytest.mark.parametrize(
    ("spoil", "text"),
    [
        (lambda world, receipt: setattr(world, "minute_index", 9), "current simulation boundary"),
        (lambda world, receipt: None, "already been recorded"),
    ],
)
def test_a_receipt_the_rule_refuses_changes_nothing_and_has_its_text(spoil: Any, text: str) -> None:
    world = _world()
    world.minute_index = 3
    receipt = _receipt(world, 1, "requested")
    if "already" in text:
        record_shared_operation(world, receipt)
    spoil(world, receipt)
    before = _text(world.model_state[SHARED])
    with pytest.raises(native.Unsupported):
        native.rules("common_operations").record_shared_operation(world, receipt)
    assert _text(world.model_state[SHARED]) == before
    with pytest.raises(ValueError, match=text):
        record_shared_operation(world, receipt)
    assert _text(world.model_state[SHARED]) == before


def test_an_order_or_identity_the_rule_refuses_is_its_own() -> None:
    world = _world()
    world.minute_index = 3
    record_shared_operation(world, _receipt(world, 1, "requested"))
    with pytest.raises(ValueError, match="out of order"):
        record_shared_operation(world, _receipt(world, 1, "completed"))
    with pytest.raises(ValueError, match="out of order"):
        record_shared_operation(world, _receipt(world, 2, "assigned"))
    other = make_care_transition_record(
        world,
        request_id="request-1",
        agent_id=sorted(world.agents)[1],
        household_id=None,
        support_kind="emotional_support",
        status="assigned",
    )
    world.event_log.append(other[0])
    with pytest.raises(ValueError, match="identity changed"):
        record_shared_operation(world, other[1])


def test_a_state_change_that_is_not_the_world_s_is_the_rules_to_refuse() -> None:
    world = _world()
    world.minute_index = 3
    agent = sorted(world.agents)[0]
    for status in ("requested", "started"):
        record_shared_operation(world, _receipt(world, 1, status, staff_id=None))
    change = NumericStateChange(
        entity_id=agent,
        entity_kind="agent",
        field_path="stress",
        before=0.5,
        after=world.agents[agent].stress + 0.25,
        unit="index",
    )
    receipt = _receipt(
        world, 1, "completed", outcome_observation="observed_change", state_changes=(change,)
    )
    with pytest.raises(ValueError, match="does not match the current saved"):
        record_shared_operation(world, receipt)


def test_a_restored_receipt_is_read_by_the_python_rule_once_and_then_trusted() -> None:
    world = _world()
    world.minute_index = 3
    record_shared_operation(world, _receipt(world, 1, "requested"))
    request = _request(world, 1, sorted(world.agents)[0], 3)
    del world.support_requests[request.id]
    world.support_requests["request-1"] = SupportRequestState(
        id="request-1",
        agent_id=sorted(world.agents)[0],
        requested_minute=3,
        timeout_minute=99,
    )
    assert _TICK_VALID(world)

    copied = copy.deepcopy(world)
    assert not _TICK_VALID(copied)
    kernel = native.rules("common_operations").record_shared_operation
    with pytest.raises(native.Unsupported):
        kernel(copied, _receipt(copied, 2, "requested"))
    copied.model_state[SHARED]["support_requests"].clear()
    world_two = copy.deepcopy(world)
    assert not _TICK_VALID(world_two)
    validate_shared_operations_state(world_two, validate_event_history=False)
    assert _TICK_VALID(world_two)
    world_two.minute_index = 4
    kernel(world_two, _receipt(world_two, 3, "requested"))
    assert _mirror(world_two, SHARED).verify()


def test_a_passed_staff_receipt_stays_with_the_python_rule() -> None:
    """Its rule also reads the retained staff history, which may have changed since."""
    world = _world()
    state = world.model_state[SHARED]
    state["history"].append(
        {"event_type": "staff_operation_transition", "minute": 0, "event_id": "e-1"}
    )
    _TICK_PASSED(world)
    assert not _TICK_VALID(world)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda state, world: state.__setitem__("schema_version", 2),
        lambda state, world: state.__setitem__("policy_version", "other"),
        lambda state, world: state["support_requests"]["request-1"].__setitem__(
            "status", "completed"
        ),
        lambda state, world: state["support_requests"]["request-1"].__setitem__(
            "agent_id", "not one!"
        ),
        lambda state, world: state["support_requests"]["request-1"].__setitem__(
            "support_kind", "x"
        ),
        lambda state, world: state["support_requests"]["request-1"].__setitem__(
            "created_minute", 99
        ),
        lambda state, world: state["support_requests"].__setitem__(
            "other", dict(state["support_requests"]["request-1"])
        ),
        lambda state, world: world.support_requests.pop("request-1"),
        lambda state, world: setattr(
            world.support_requests["request-1"], "status", SupportRequestStatus.ASSIGNED
        ),
        lambda state, world: setattr(world.support_requests["request-1"], "agent_id", "someone"),
        lambda state, world: state["history"].append({"minute": 99}),
        lambda state, world: state["history"][0].__setitem__("minute", 2),
    ],
)
def test_the_tick_validator_takes_nothing_python_turns_down(spoil: Any) -> None:
    world = _world()
    world.minute_index = 3
    record_shared_operation(world, _receipt(world, 1, "requested"))
    world.support_requests["request-1"] = SupportRequestState(
        id="request-1",
        agent_id=sorted(world.agents)[0],
        requested_minute=3,
        timeout_minute=99,
    )
    assert _TICK_VALID(world)
    spoil(world.model_state[SHARED], world)
    with pytest.raises(ValueError):
        validate_shared_operations_state(world, validate_event_history=False)
    try:
        assert _TICK_VALID(world) is False
    except native.Unsupported:
        pass


def test_the_tick_validator_reads_who_has_an_assigned_request() -> None:
    world = _world()
    world.minute_index = 3
    record_shared_operation(world, _receipt(world, 1, "requested"))
    record_shared_operation(world, _receipt(world, 1, "assigned", staff_id="S01"))
    request = SupportRequestState(
        id="request-1",
        agent_id=sorted(world.agents)[0],
        requested_minute=3,
        timeout_minute=99,
        status=SupportRequestStatus.ASSIGNED,
        assigned_staff_id="S01",
    )
    world.support_requests["request-1"] = request
    assert _TICK_VALID(world)
    validate_shared_operations_state(world, validate_event_history=False)
    request.assigned_staff_id = "S02"
    assert _TICK_VALID(world) is False
    with pytest.raises(ValueError, match="assigned staff disagrees"):
        validate_shared_operations_state(world, validate_event_history=False)
