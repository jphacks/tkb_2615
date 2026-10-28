"""Typed presence changes on a generated Scenario: a relocation walks the person to the
new zone and survives a resume, and a reunion records the household. Each person is
chosen from the state at the fork, as an operator would pick them.

Defects found while covering these paths (#178) are fixed and checked here: a
reunion that includes someone waiting for support keeps that request in its household
(#179), a return or an arrival waits while someone walks to the full zone (#180), and
an exit of someone busy waits until they are free (#181). A failed precondition calls
pytest.fail, so it is never mistaken for a defect.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.models import Activity
from sheltersim.events.effects import EffectProgram, SupportMode, TargetSelector, TimedAction
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.storage.projects import digest

END = 120
FORK_MINUTE = 10
RETURN_MINUTE = 20
FREE = (Activity.IDLE, Activity.RESTING, Activity.SLEEPING)


def _presence(
    effect_id: str, selector: TargetSelector, start: int, **parameters: Any
) -> EffectProgram:
    return EffectProgram(
        program_id=f"program-{effect_id}",
        narrative=effect_id,
        source_text=effect_id,
        simulation_start_minute=start,
        effects=[
            TimedAction(
                effect_id=effect_id,
                op="population_presence",
                target_selector=selector,
                start_minute=start,
                parameters=parameters,
                units="population_transition",
                source_id="TEST-PRESENCE",
                assumption_ids=[],
                policy_dependencies=["population_lifecycle_v1"],
                reversible=False,
            )
        ],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


def _fork(timeline: TimelineStore, source: str, branch_id: str, program: EffectProgram) -> None:
    timeline.fork(
        source,
        program.simulation_start_minute,
        branch_id,
        program=program,
        approved_program_hash=digest(program.model_dump(mode="json")),
    )


def _person(agent_id: str) -> TargetSelector:
    return TargetSelector(kind="person", target_id=agent_id)


def _history(world: WorldState) -> list[tuple[int, str, str]]:
    rows = world.model_state["population_lifecycle_v1"]["history"]
    return [(row["minute"], row["agent_id"], row["action"]) for row in rows]


def _scenario(generated_previews, **zone_capacities: int) -> ScenarioSpec:
    document = copy.deepcopy(generated_previews(duration_days=END / 1440)["scenario"])
    for zone in document["zones"]:
        zone["capacity"] = zone_capacities.get(zone["id"], zone["capacity"])
    return ScenarioSpec.model_validate(document)


@pytest.fixture(scope="module")
def main_line(tmp_path_factory, generated_previews, saved_timeline):
    """The generated Scenario's main line at the fork minute; tests fork their own Branches."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
        store, timeline = saved_timeline(
            tmp_path_factory.mktemp("presence") / "projects", _scenario(generated_previews)
        )
        timeline.advance("main", FORK_MINUTE, checkpoint_interval=FORK_MINUTE)
        yield store, timeline


def _at_fork(timeline: TimelineStore) -> WorldState:
    return timeline.seek("main", FORK_MINUTE).world


def _co_located_pair(world: WorldState, *, waiting_for_support: bool) -> list[str]:
    """Two people in one zone; one waits for support only when asked to."""
    by_zone: dict[str, list[str]] = {}
    for agent_id, agent in sorted(world.agents.items()):
        by_zone.setdefault(agent.zone_id, []).append(agent_id)
    for members in by_zone.values():
        waiting = [item for item in members if world.agents[item].support_request_id]
        others = [item for item in members if not world.agents[item].support_request_id]
        if waiting_for_support and waiting and len(members) >= 2:
            return sorted([waiting[0], next(item for item in members if item != waiting[0])])
        if not waiting_for_support and len(others) >= 2:
            return others[:2]
    pytest.fail("no two co-located people match at the fork minute")


def test_a_relocation_walks_the_person_to_the_new_zone_and_survives_a_resume(main_line):
    store, timeline = main_line
    world = _at_fork(timeline)
    mover = next(
        (key for key, agent in sorted(world.agents.items()) if agent.activity in FREE), None
    )
    if mover is None:
        pytest.fail("nobody is free to move at the fork minute")
    destination = next(zone for zone in ("quiet", "medical") if world.agents[mover].zone_id != zone)
    program = _presence(
        "relocate", _person(mover), FORK_MINUTE, action="relocate", zone_id=destination
    )

    _fork(timeline, "main", "relocated", program)
    continuous = timeline.advance("relocated", END, checkpoint_interval=30).world
    # Each advance saves its last minute; a fresh store resumes from that Checkpoint,
    # including one taken while the person is still walking.
    _fork(timeline, "main", "relocated-in-steps", program)
    for minute in (FORK_MINUTE + 1, FORK_MINUTE + 2, FORK_MINUTE + 5, END):
        TimelineStore(store, "p", "r").advance(
            "relocated-in-steps", minute, checkpoint_interval=END
        )
    stepped = TimelineStore(store, "p", "r").seek("relocated-in-steps", END).world

    ((minute, agent_id, action),) = _history(continuous)
    assert (agent_id, action) == (mover, "relocated")
    assert minute > FORK_MINUTE, "the person arrives after walking, not at the fork"
    assert continuous.model_state["population_lifecycle_v1"]["pending_relocations"] == {}
    # The arrival is applied during that minute's tick, so it shows from the next minute.
    arrived = timeline.seek("relocated", minute + 1).world
    assert arrived.agents[mover].zone_id == destination
    assert json.dumps(stepped.to_dict(), sort_keys=True) == json.dumps(
        continuous.to_dict(), sort_keys=True
    )


def test_a_reunion_records_the_household_of_everyone_in_it(main_line):
    _, timeline = main_line
    pair = _co_located_pair(_at_fork(timeline), waiting_for_support=False)
    household = _at_fork(timeline).agents[pair[0]].profile.group_id
    program = _presence(
        "reunite",
        TargetSelector(kind="person", candidate_ids=pair),
        FORK_MINUTE,
        action="reunite",
        household_id=household,
    )

    _fork(timeline, "main", "reunited", program)
    world = timeline.advance("reunited", END, checkpoint_interval=30).world

    assert _history(world) == [
        (FORK_MINUTE, pair[0], "reunited"),
        (FORK_MINUTE, pair[1], "reunited"),
    ]
    people = world.model_state["population_presence_v1"]["people"]
    assert {people[agent_id]["household_id"] for agent_id in pair} == {household}


def test_a_reunion_keeps_an_open_support_request_in_its_household(main_line):
    """The run used to stop when staff took up the request after the reunion (#179)."""
    _, timeline = main_line
    world = _at_fork(timeline)
    pair = _co_located_pair(world, waiting_for_support=True)
    waiting = next(item for item in pair if world.agents[item].support_request_id)
    other = next(item for item in pair if item != waiting)
    request_household = world.agents[waiting].profile.group_id
    household = world.agents[other].profile.group_id
    if household == request_household:
        pytest.fail("the reunion must move the waiting person to another household")
    request_id = world.agents[waiting].support_request_id
    program = _presence(
        "reunite-waiting",
        TargetSelector(kind="person", candidate_ids=pair),
        FORK_MINUTE,
        action="reunite",
        household_id=household,
    )

    _fork(timeline, "main", "reunited-waiting", program)
    world = timeline.advance("reunited-waiting", END, checkpoint_interval=30).world

    people = world.model_state["population_presence_v1"]["people"]
    assert people[waiting]["household_id"] == household
    transitions = [
        (row["status"], row["household_id"])
        for row in world.model_state["shared_operations_v1"]["history"]
        if row.get("request_id") == request_id
    ]
    assert len(transitions) > 1, "staff never took up the request after the reunion"
    assert {status for status, _ in transitions} >= {"requested", "assigned"}
    assert {item for _, item in transitions} == {request_household}


def test_an_exit_of_someone_waiting_for_support_waits_until_they_are_free(main_line):
    """The exit used to fail at once and never happen (#181)."""
    _, timeline = main_line
    world = _at_fork(timeline)
    leaver = next(
        (key for key, agent in sorted(world.agents.items()) if agent.support_request_id), None
    )
    if leaver is None:
        pytest.fail("nobody waits for support at the fork minute")
    request_id = world.agents[leaver].support_request_id
    program = _presence("exit-waiting", _person(leaver), FORK_MINUTE, action="temporary_exit")

    _fork(timeline, "main", "exit-waiting", program)
    world = timeline.advance("exit-waiting", END, checkpoint_interval=30).world

    assert leaver not in world.agents
    ((exit_minute, _, _),) = [row for row in _history(world) if row[1] == leaver]
    attempts = [
        record
        for record in world.event_log.records
        if record.event.event_type == "effect_program_population"
        and record.realized_minute >= FORK_MINUTE
    ]
    assert attempts[0].realized_minute == FORK_MINUTE
    assert {record.status.value for record in attempts[:-1]} == {"suppressed"}
    assert "waits until the person is free" in attempts[0].detail
    assert (attempts[-1].status.value, attempts[-1].realized_minute) == ("applied", exit_minute)
    request_ends = [
        row["minute"]
        for row in world.model_state["shared_operations_v1"]["history"]
        if row.get("request_id") == request_id and row["status"] in {"completed", "interrupted"}
    ]
    assert request_ends and request_ends[0] <= exit_minute, "left only after the request ended"


def test_an_exit_still_waiting_at_its_expected_return_is_dropped(main_line):
    """Leaving after the time to be back makes no sense, so the deferred exit ends (#181)."""
    _, timeline = main_line
    world = _at_fork(timeline)
    leaver = next(
        (key for key, agent in sorted(world.agents.items()) if agent.support_request_id), None
    )
    if leaver is None:
        pytest.fail("nobody waits for support at the fork minute")
    program = _presence(
        "exit-short",
        _person(leaver),
        FORK_MINUTE,
        action="temporary_exit",
        expected_return_minute=FORK_MINUTE + 1,
    )

    _fork(timeline, "main", "exit-short", program)
    world = timeline.advance("exit-short", FORK_MINUTE + 5, checkpoint_interval=30).world

    assert leaver in world.agents
    attempts = [
        record
        for record in world.event_log.records
        if record.event.event_type == "effect_program_population"
        and record.realized_minute >= FORK_MINUTE
    ]
    assert [record.realized_minute for record in attempts] == [10, 11, 12]
    assert {record.status.value for record in attempts} == {"suppressed"}
    assert "not free before the expected return" in attempts[-1].detail


@pytest.fixture
def one_place_staff_zone(tmp_path, monkeypatch, generated_previews, saved_timeline):
    """The generated Scenario with a one-place staff zone, at the fork minute."""
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    _, timeline = saved_timeline(tmp_path / "projects", _scenario(generated_previews, staff=1))
    timeline.advance("main", FORK_MINUTE, checkpoint_interval=FORK_MINUTE)
    return timeline


def _walking_to_empty_staff_zone(world: WorldState) -> list[str]:
    walking = [key for key, agent in world.agents.items() if agent.target_id == "staff"]
    if any(agent.zone_id == "staff" for agent in world.agents.values()) or not walking:
        pytest.fail("the staff zone must be empty with someone walking to it")
    return walking


def test_a_return_waits_while_someone_is_walking_to_the_full_zone(one_place_staff_zone):
    """The return used to take the place of the walker, whose arrival overfilled it (#180)."""
    timeline = one_place_staff_zone
    world = _at_fork(timeline)
    leaver = next(
        (
            key
            for key, agent in sorted(world.agents.items())
            if agent.activity in FREE and not agent.support_request_id
        ),
        None,
    )
    if leaver is None:
        pytest.fail("nobody is free to leave at the fork minute")
    _fork(
        timeline,
        "main",
        "away",
        _presence("exit", _person(leaver), FORK_MINUTE, action="temporary_exit"),
    )
    away = timeline.advance("away", RETURN_MINUTE, checkpoint_interval=RETURN_MINUTE).world
    _walking_to_empty_staff_zone(away)
    _fork(
        timeline,
        "away",
        "back",
        _presence("back", _person(leaver), RETURN_MINUTE, action="return", zone_id="staff"),
    )

    world = timeline.advance("back", END, checkpoint_interval=30).world

    history = [
        (minute, action) for minute, agent_id, action in _history(world) if agent_id == leaver
    ]
    assert history[:2] == [(FORK_MINUTE, "temporary_exit"), (RETURN_MINUTE, "waiting")]
    ((admitted_at, _),) = [row for row in history[2:] if row[1] == "admitted"]
    assert admitted_at > RETURN_MINUTE
    assert world.model_state["population_lifecycle_v1"]["pending_returns"] == []
    assert timeline.seek("back", admitted_at + 1).world.agents[leaver].zone_id == "staff"


def test_an_arrival_waits_while_someone_is_walking_to_the_full_zone(one_place_staff_zone):
    """Arrival admission ignored the walker in the same way (#180)."""
    timeline = one_place_staff_zone
    away = timeline.advance("main", RETURN_MINUTE, checkpoint_interval=RETURN_MINUTE).world
    walkers = _walking_to_empty_staff_zone(away)
    residents = set(away.agents)
    program = EffectProgram(
        program_id="program-arrival",
        narrative="arrival",
        source_text="arrival",
        simulation_start_minute=RETURN_MINUTE,
        effects=[
            TimedAction(
                effect_id="arrival",
                op="add_evacuees",
                target_selector=TargetSelector(kind="zone", target_id="staff"),
                start_minute=RETURN_MINUTE,
                parameters={"count": 1, "household_size": 1},
                units="evacuee",
                source_id="TEST-ARRIVAL",
                assumption_ids=[],
                policy_dependencies=["population_dynamics_v1"],
                reversible=False,
            )
        ],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )
    _fork(timeline, "main", "arrival", program)

    world = timeline.advance("arrival", END, checkpoint_interval=30).world

    (newcomer,) = set(world.agents) - residents
    in_staff = [
        (
            item.minute_index,
            sorted(key for key, agent in item.agents.items() if agent.zone_id == "staff"),
        )
        for item in timeline.iter_worlds("arrival", RETURN_MINUTE, END)
    ]
    first_walker_minute = next(minute for minute, ids in in_staff if set(ids) & set(walkers))
    assert newcomer not in dict(in_staff)[first_walker_minute]
    admitted = [minute for minute, ids in in_staff if newcomer in ids]
    assert admitted and admitted[0] > first_walker_minute, "admitted once there is room"
