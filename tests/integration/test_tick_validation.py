"""The end-of-tick check skips only what is provably unchanged since the last one.

Every state a skipped check or a Rust acceptance could hide still stops the tick with
the error of the plain per-tick validation, and what is left to the full validation
is only old history. Without the Rust kernels the check is the plain validation, so
these tests hold in both modes. That the kernels accept only what the Python rules
accept is checked in tests/integration/test_native_kernels.py.
"""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from sheltersim import native
from sheltersim.core.engine import TickEngine, TickPhase
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.models import SupportRequestState, SupportRequestStatus
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.experiments.scenario import ScenarioSpec


@pytest.fixture(scope="module")
def finished_world(tmp_path_factory, generated_scenario_preview) -> dict:
    scenario = ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    return run_scenario(scenario, tmp_path_factory.mktemp("run")).to_dict()


def _restored(document: dict) -> WorldState:
    return WorldState.from_dict(copy.deepcopy(document))


def _checked_world(document: dict) -> WorldState:
    """A world whose end-of-tick check has run twice, so the memo holds everything."""
    world = _restored(document)
    world.validate_after_tick()
    world.validate_after_tick()
    return world


def _settled(world: WorldState) -> SupportRequestState:
    return next(
        request
        for request in world.support_requests.values()
        if request.status in (SupportRequestStatus.RESOLVED, SupportRequestStatus.UNRESOLVED)
    )


def _first(mapping: dict):
    return next(iter(mapping.values()))


def _set_settled(**fields):
    def corrupt(world: WorldState) -> None:
        request = _settled(world)
        for name, value in fields.items():
            setattr(request, name, value)

    return corrupt


def _replace_settled(world: WorldState) -> None:
    request = _settled(world)
    world.support_requests[request.id] = replace(request, agent_id="nobody")


def _add_broken_request(world: WorldState) -> None:
    world.support_requests["new-request"] = SupportRequestState(
        id="new-request",
        agent_id="nobody",
        requested_minute=0,
        timeout_minute=1,
        status=SupportRequestStatus.UNRESOLVED,
    )


def _resource_config(world: WorldState) -> None:
    world.model_configs["resource_service"]["water_ml_per_service"] = -1


def _information_config(world: WorldState) -> None:
    world.model_configs["information_spread"]["unknown"] = 1


def _policy_registry(world: WorldState) -> None:
    world.model_configs["policy_registry"]["psychology"] = "unknown-policy"


def _geometry_config(world: WorldState) -> None:
    del world.model_configs["facility_geometry"]


def _body_config(world: WorldState) -> None:
    world.model_state["body_state_v1"]["config"]["unknown"] = 1


def _duplicate_reservation_id(world: WorldState) -> None:
    ids = world.model_state["resource_ledger_v1"]["used_reservation_ids"]
    ids.append(ids[0])


def _reservation_id_of_wrong_type(world: WorldState) -> None:
    world.model_state["resource_ledger_v1"]["used_reservation_ids"].append(7)


def _old_reservation_id(world: WorldState) -> None:
    world.model_state["resource_ledger_v1"]["used_reservation_ids"][0] = 7


def _reservation_id_list_copied(world: WorldState) -> None:
    ledger = world.model_state["resource_ledger_v1"]
    ledger["used_reservation_ids"] = [*ledger["used_reservation_ids"], 7]


def _presence(world: WorldState) -> None:
    _first(world.model_state["population_presence_v1"]["people"])["status"] = "departed"


def _lifecycle(world: WorldState) -> None:
    world.model_state["population_lifecycle_v1"]["pending_returns"].append({"agent_id": "nobody"})


def _overlay(world: WorldState) -> None:
    world.model_state["geometry_overlay_v1"]["partitions"]["x"] = {"zone_id": "nowhere"}


def _operation_newest_changed(world: WorldState) -> None:
    world.model_state["shared_operations_v1"]["history"][-1]["event_id"] = "unknown-event"


def _operation_new_broken(world: WorldState) -> None:
    history = world.model_state["shared_operations_v1"]["history"]
    record = copy.deepcopy(history[-1])
    record["minute"] = world.minute_index
    record["event_id"] = "unknown-event"
    history.append(record)


def _operation_header(world: WorldState) -> None:
    world.model_state["shared_operations_v1"]["support_requests"]["unknown"] = 1


def _social_trust(world: WorldState) -> None:
    _first(world.model_state["social_network_v1"]["relationships"])["trust"] = 2.0


def _social_key(world: WorldState) -> None:
    relationships = world.model_state["social_network_v1"]["relationships"]
    relationships["["] = copy.deepcopy(_first(relationships))


def _body_relationship(world: WorldState) -> None:
    _first(world.model_state["body_state_v1"]["relationships"])["contact_count"] = -1


def _body_relationship_topic(world: WorldState) -> None:
    _first(world.model_state["body_state_v1"]["relationships"])["topic_ids"].append(1)


def _body_relationship_key(world: WorldState) -> None:
    relationships = world.model_state["body_state_v1"]["relationships"]
    relationships['["nobody","nobody-else"]'] = copy.deepcopy(_first(relationships))


def _social_record(world: WorldState) -> dict:
    return _first(world.model_state["social_network_v1"]["relationships"])


def _record_update(world: WorldState) -> None:
    _social_record(world).update(trust=2.0)


def _record_pop(world: WorldState) -> None:
    _social_record(world).pop("trust")


def _record_clear(world: WorldState) -> None:
    _social_record(world).clear()


def _record_setdefault(world: WorldState) -> None:
    _social_record(world).setdefault("extra", 1)


def _record_del(world: WorldState) -> None:
    del _social_record(world)["last_contact_minute"]


def _record_replaced(world: WorldState) -> None:
    relationships = world.model_state["social_network_v1"]["relationships"]
    key = next(iter(relationships))
    relationships[key] = {**relationships[key], "trust": 2.0}


def _record_added(world: WorldState) -> None:
    relationships = world.model_state["social_network_v1"]["relationships"]
    first, second = sorted(world.agents)[:2]
    relationships[f'["{first}","{second}x"]'] = {**_first(relationships), "contact_count": -1}


def _relationships_replaced(world: WorldState) -> None:
    state = world.model_state["social_network_v1"]
    state["relationships"] = {key: {} for key in state["relationships"]}


def _relationships_moved(world: WorldState) -> None:
    """A key goes to the end of the dict, with a broken record."""
    relationships = world.model_state["social_network_v1"]["relationships"]
    key = next(iter(relationships))
    record = relationships.pop(key)
    relationships[key] = {**record, "contact_count": -1}


def _body_topic_replaced(world: WorldState) -> None:
    _first(world.model_state["body_state_v1"]["relationships"])["topic_ids"] = [1]


def _body_topic_too_long(world: WorldState) -> None:
    _first(world.model_state["body_state_v1"]["relationships"])["topic_ids"].extend(["a"] * 33)


def _body_agent(world: WorldState) -> None:
    _first(world.model_state["body_state_v1"]["agents"])["sleep_debt_minutes"] = 1441


def _psychology(world: WorldState) -> None:
    record = _first(world.model_state["psychological_distress_v1"]["agents"])
    record["daily_high_distress_minutes"] = record["daily_observed_minutes"] + 1


def _agent_dynamics(world: WorldState) -> None:
    _first(world.model_state["agent_dynamics_v1"]["agents"])["known_facility_ids"] = ["nowhere"]


def _movement(world: WorldState) -> None:
    _first(world.model_state["movement_v1"]["positions"])["progress"] = 2


def _query_pressure(world: WorldState) -> None:
    pressure = world.model_state["information_spread_v1"]["query_pressure"]
    pressure[next(iter(pressure))] = 2.0


def _contact(world: WorldState) -> None:
    contacts = world.model_state["information_spread_v1"]["contacts"]
    first, second = sorted(world.agents)[:2]
    contacts[f'["{first}","{second}"]'] = {"last_seen_minute": 0, "consecutive_minutes": 2}


def _contact_key(world: WorldState) -> None:
    contacts = world.model_state["information_spread_v1"]["contacts"]
    contacts['["nobody","nobody-else"]'] = {"last_seen_minute": 0, "consecutive_minutes": 1}


def _set_agent(**fields):
    def corrupt(world: WorldState) -> None:
        agent = _first(world.agents)
        for name, value in fields.items():
            setattr(agent, name, value)

    return corrupt


def _agent_need(world: WorldState) -> None:
    _first(world.agents).needs.hunger = 1.5


CORRUPTIONS = {
    "config of the resource service": _resource_config,
    "config of information spread": _information_config,
    "policy registry": _policy_registry,
    "geometry config": _geometry_config,
    "saved body config": _body_config,
    "settled request: unknown status": _set_settled(status="unknown"),
    "settled request: reopened": _set_settled(status=SupportRequestStatus.PENDING),
    "settled request: assigned": _set_settled(status=SupportRequestStatus.ASSIGNED),
    "settled request: in the future": _set_settled(requested_minute=10**9),
    "settled request: unknown Agent": _set_settled(agent_id="nobody"),
    "settled request: bad attempt": _set_settled(last_assignment_attempt_minute=-1),
    "settled request: bad wait reason": _set_settled(last_assignment_wait_reason_code="unknown"),
    "settled request: replaced object": _replace_settled,
    "new request": _add_broken_request,
    "reservation ID repeated": _duplicate_reservation_id,
    "reservation ID not a string": _reservation_id_of_wrong_type,
    "earlier reservation ID changed": _old_reservation_id,
    "reservation ID in a copied list": _reservation_id_list_copied,
    "presence": _presence,
    "lifecycle": _lifecycle,
    "geometry overlay": _overlay,
    "newest operation record changed": _operation_newest_changed,
    "new operation record": _operation_new_broken,
    "operations state besides its history": _operation_header,
    "social relationship": _social_trust,
    "social relationship key": _social_key,
    "body relationship": _body_relationship,
    "body relationship topic": _body_relationship_topic,
    "body relationship key": _body_relationship_key,
    "social record: update": _record_update,
    "social record: pop": _record_pop,
    "social record: clear": _record_clear,
    "social record: setdefault": _record_setdefault,
    "social record: del": _record_del,
    "social record: replaced": _record_replaced,
    "social record: added": _record_added,
    "social relationships replaced": _relationships_replaced,
    "social relationship moved": _relationships_moved,
    "body relationship topic replaced": _body_topic_replaced,
    "body relationship topics too many": _body_topic_too_long,
    "body Agent": _body_agent,
    "psychology": _psychology,
    "agent dynamics": _agent_dynamics,
    "position": _movement,
    "query pressure": _query_pressure,
    "contact": _contact,
    "contact key": _contact_key,
    "Agent zone": _set_agent(zone_id="nowhere"),
    "Agent activity": _set_agent(activity="unknown"),
    "Agent stress": _set_agent(stress=1.5),
    "Agent trust": _set_agent(trust=float("nan")),
    "Agent decision time": _set_agent(next_decision_at=-1),
    "Agent cooldown": _set_agent(support_cooldown_until=True),
    "Agent needs": _agent_need,
}


@pytest.mark.parametrize("name", CORRUPTIONS)
def test_a_corruption_stops_the_tick_with_the_plain_check_s_error(finished_world, name) -> None:
    corrupt = CORRUPTIONS[name]
    plain = _restored(finished_world)
    corrupt(plain)
    with pytest.raises(StateInvariantError) as expected:
        plain.validate(validate_event_history=False)

    world = _checked_world(finished_world)
    corrupt(world)
    with pytest.raises(StateInvariantError) as raised:
        world.validate_after_tick()
    assert str(raised.value) == str(expected.value)


def _old_operation_record(world: WorldState) -> None:
    world.model_state["shared_operations_v1"]["history"][0]["event_id"] = "unknown-event"


def test_old_history_is_left_to_the_next_full_validation(finished_world) -> None:
    world = _checked_world(finished_world)
    _old_operation_record(world)

    world.validate_after_tick()
    with pytest.raises(StateInvariantError):
        world.validate()


def test_the_full_validation_reads_a_settled_request_the_tick_check_trusts(finished_world) -> None:
    """Only a write around the attribute protocol can pass the tick check; a save stops it."""
    if not native.available():
        pytest.skip("the plain validation reads every request")
    world = _checked_world(finished_world)
    _settled(world).__dict__["agent_id"] = "nobody"

    world.validate_after_tick()
    with pytest.raises(StateInvariantError):
        world.validate()


def _engine_at_the_end_of(document: dict, handler) -> TickEngine:
    world = _restored(document)
    world.duration_minutes += 10
    return TickEngine(world, phase_handler=handler)


def test_a_phase_that_writes_a_settled_request_stops_that_tick(finished_world) -> None:
    corrupt = {"now": False}

    def handler(phase, world, minute, rng) -> None:
        if corrupt["now"] and phase is TickPhase.COLLECT_METRICS:
            _settled(world).agent_id = "nobody"

    engine = _engine_at_the_end_of(finished_world, handler)
    engine.step()
    engine.step()
    corrupt["now"] = True
    with pytest.raises(StateInvariantError):
        engine.step()


def test_a_phase_that_writes_a_config_stops_that_tick(finished_world) -> None:
    corrupt = {"now": False}

    def handler(phase, world, minute, rng) -> None:
        if corrupt["now"] and phase is TickPhase.COLLECT_METRICS:
            _resource_config(world)

    engine = _engine_at_the_end_of(finished_world, handler)
    engine.step()
    corrupt["now"] = True
    with pytest.raises(StateInvariantError):
        engine.step()


def test_a_request_settled_and_written_again_is_checked_again(finished_world) -> None:
    world = _checked_world(finished_world)
    request = _settled(world)
    request.cooldown_until = request.cooldown_until + 1
    world.validate_after_tick()
    world.validate_after_tick()
    request.cooldown_until = -1
    with pytest.raises(StateInvariantError):
        world.validate_after_tick()


def test_a_world_whose_contents_are_swapped_starts_a_new_memo(finished_world) -> None:
    """Interventions replace the world's contents with a checked copy (events/effects.py)."""
    world = _checked_world(finished_world)
    candidate = WorldState.from_dict(world.to_dict())
    world.__dict__.clear()
    world.__dict__.update(candidate.__dict__)

    world.validate_after_tick()
    _settled(world).agent_id = "nobody"
    with pytest.raises(StateInvariantError):
        world.validate_after_tick()


def test_a_copied_world_is_checked_on_its_own(finished_world) -> None:
    world = _checked_world(finished_world)
    # The EventLog is shared; only the model state and requests matter here.
    twin = copy.deepcopy(world, {id(world.event_log): world.event_log})

    twin.validate_after_tick()
    _settled(twin).agent_id = "nobody"
    with pytest.raises(StateInvariantError):
        twin.validate_after_tick()
    world.validate_after_tick()


def test_the_memo_is_not_kept_after_a_failed_check(finished_world) -> None:
    world = _checked_world(finished_world)
    _resource_config(world)
    with pytest.raises(StateInvariantError):
        world.validate_after_tick()
    assert "_tick_memo" not in world.__dict__


def test_a_memo_does_not_carry_over_when_the_clock_goes_back(finished_world) -> None:
    world = _checked_world(finished_world)
    before = world.__dict__.get("_tick_memo")
    world.minute_index -= 1
    world.validate_after_tick()
    assert native.available() is (before is not None)
    assert before is None or world.__dict__["_tick_memo"] is not before


def test_changing_the_agents_checks_settled_requests_again(finished_world) -> None:
    """A settled request's rules read the Agent IDs, so a change of IDs rereads them all."""
    if not native.available():
        pytest.skip("the plain validation reads every request")
    world = _checked_world(finished_world)
    memo = world.__dict__["_tick_memo"]
    memo.begin(world)
    assert len(memo.requests_to_check(world.support_requests)) < len(world.support_requests)
    memo.agents_unchanged = False
    assert len(memo.requests_to_check(world.support_requests)) == len(world.support_requests)


def test_the_validation_leaves_the_saved_state_unchanged(finished_world) -> None:
    """The memo and the write reports are not state: the world's JSON is the same."""
    world = _restored(finished_world)
    before = world.canonical_json()
    world.validate_after_tick()
    world.validate_after_tick()
    assert world.canonical_json() == before
    assert world.same_state(_restored(finished_world))


def _unsettled_scan(world: WorldState) -> list[SupportRequestState]:
    return [
        request
        for request in world.support_requests.values()
        if request.status in (SupportRequestStatus.PENDING, SupportRequestStatus.ASSIGNED)
    ]


def _assert_scans_agree(world: WorldState) -> None:
    """What a caller keeps of the kept list is what it keeps of every request, in order."""
    kept = [
        request
        for request in world.unsettled_support_requests()
        if request.status in (SupportRequestStatus.PENDING, SupportRequestStatus.ASSIGNED)
    ]
    expected = _unsettled_scan(world)
    assert [id(request) for request in kept] == [id(request) for request in expected]


def test_the_unsettled_requests_equal_a_scan_in_every_tick_phase(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 480
    original = OfflineSimulationController._run_phase
    open_seen = [0]

    def checked(self, phase, world, minute, rng):
        original(self, phase, world, minute, rng)
        _assert_scans_agree(world)
        open_seen[0] += len(_unsettled_scan(world))

    monkeypatch.setattr(OfflineSimulationController, "_run_phase", checked)
    run_scenario(ScenarioSpec.model_validate(document), tmp_path)
    assert open_seen[0] > 0


def test_a_reopened_request_is_seen_in_the_order_of_the_dict(finished_world) -> None:
    world = _checked_world(finished_world)
    settled = [
        request
        for request in world.support_requests.values()
        if request.status in (SupportRequestStatus.RESOLVED, SupportRequestStatus.UNRESOLVED)
    ]
    request = settled[len(settled) // 2]
    request.status = SupportRequestStatus.PENDING
    _assert_scans_agree(world)
    assert request in _unsettled_scan(world)
    # The check rejects the state (no timeout event); a kept memo must not hide it either way.
    memo = world.__dict__.get("_tick_memo")
    if memo is not None:
        memo.begin(world)
        memo.requests_to_check(world.support_requests)
        memo.commit(world)
        _assert_scans_agree(world)
        assert memo._open_requests == [
            item
            for item in world.support_requests.values()
            if item.status not in (SupportRequestStatus.RESOLVED, SupportRequestStatus.UNRESOLVED)
        ]


def test_an_added_request_is_seen_before_the_next_check(finished_world) -> None:
    world = _checked_world(finished_world)
    world.support_requests["new-request"] = SupportRequestState(
        id="new-request",
        agent_id=next(iter(world.agents)),
        requested_minute=0,
        timeout_minute=1,
    )
    _assert_scans_agree(world)
    assert world.support_requests["new-request"] in list(world.unsettled_support_requests())


def test_replaced_or_removed_requests_are_not_read_from_the_memo(finished_world) -> None:
    world = _checked_world(finished_world)
    request = _settled(world)
    # A new object with an open status in the place of a settled one.
    world.support_requests[request.id] = replace(request, status=SupportRequestStatus.PENDING)
    _assert_scans_agree(world)
    assert world.support_requests[request.id] in _unsettled_scan(world)

    world = _checked_world(finished_world)
    last_key = next(reversed(world.support_requests))
    del world.support_requests[last_key]
    _assert_scans_agree(world)

    world = _checked_world(finished_world)
    world.support_requests = {
        key: value for key, value in list(world.support_requests.items())[:-1]
    }
    _assert_scans_agree(world)


def test_a_swapped_or_copied_world_reads_its_own_requests(finished_world) -> None:
    world = _checked_world(finished_world)
    candidate = WorldState.from_dict(world.to_dict())
    world.__dict__.update(candidate.__dict__)
    _settled(world).status = SupportRequestStatus.PENDING
    _assert_scans_agree(world)

    world = _checked_world(finished_world)
    twin = copy.deepcopy(world, {id(world.event_log): world.event_log})
    _settled(twin).status = SupportRequestStatus.PENDING
    _assert_scans_agree(twin)
    _assert_scans_agree(world)
