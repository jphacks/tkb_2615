"""The views of rules/world_view.rs: every field the bits of Python's, made when a reader asks.

The phases' steps `_view_after_movement` and `_view_for_proposals` make them. Python's `from_world`
and `after_state_update` stay the comparison base, and run for whatever the kernel leaves to them.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.domain.body_state import create_body_state
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.propose import ProposePhase
from sheltersim.experiments.phases.update import UpdatePhase
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.policies import world_view
from sheltersim.policies.world_view import WorldView

pytestmark = pytest.mark.skipif(not native.available(), reason="only with the Rust kernels")


def _exact(value: Any) -> Any:
    """Equal only with the same types, the same bits, and the same order of each dict's keys."""
    if value is None or type(value) is bool:
        return value
    if type(value) is float:
        return value.hex()
    if type(value) is int:
        return ["int", value]
    if isinstance(value, str):
        return [type(value).__name__, str(value)]
    if isinstance(value, tuple):
        return ["tuple", *(_exact(item) for item in value)]
    if isinstance(value, Mapping):
        return ["map", *([key, _exact(item)] for key, item in value.items())]
    if hasattr(value, "__dataclass_fields__"):
        return [type(value).__name__, *([k, _exact(v)] for k, v in value.__dict__.items())]
    raise TypeError(type(value))


def _assert_same(rust: WorldView, python: WorldView) -> None:
    assert rust.minute_index == python.minute_index
    assert rust.state_hash == python.state_hash
    assert rust.resource_service_enabled is python.resource_service_enabled
    assert rust.autonomy_enabled is python.autonomy_enabled
    assert rust._has_population_context is python._has_population_context
    for name in ("agents", "facilities", "zone_members", "group_members"):
        assert _exact(getattr(rust, name)) == _exact(getattr(python, name)), name
    assert rust.immediate_needs() == python.immediate_needs()


def _rules() -> Any:
    return native.rules("world_view")


def _controller(world: WorldState) -> OfflineSimulationController:
    return OfflineSimulationController(world, _MetricSink(MetricsRecorder()))


def _run_comparing(
    scenario: ScenarioSpec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **options: Any
) -> dict[str, int]:
    """Run, comparing each view with Python's chain of from_world and after_state_update."""
    counts = {"update": 0, "propose": 0}
    after_movement = OfflineSimulationController._view_after_movement
    for_proposals = OfflineSimulationController._view_for_proposals
    chain: list[WorldView] = []

    def checked_after_movement(self: Any, world: WorldState) -> WorldView:
        made = after_movement(self, world)
        assert "_rust_view" in made.__dict__
        chain[:] = [WorldView.from_world(world, include_state_hash=False)]
        _assert_same(made, chain[0])
        counts["update"] += 1
        return made

    def checked_for_proposals(self: Any, world: WorldState, minute: int) -> WorldView:
        made = for_proposals(self, world, minute)
        assert "_rust_view" in made.__dict__
        _assert_same(made, WorldView.after_state_update(chain[0], world))
        _assert_same(made, WorldView.from_world(world))
        counts["propose"] += 1
        return made

    monkeypatch.setattr(OfflineSimulationController, "_view_after_movement", checked_after_movement)
    monkeypatch.setattr(OfflineSimulationController, "_view_for_proposals", checked_for_proposals)
    run_scenario(scenario, tmp_path, **options)
    return counts


def test_each_view_of_a_run_has_the_bits_of_python(
    generated_scenario: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 120
    counts = _run_comparing(
        ScenarioSpec.model_validate(document), tmp_path, monkeypatch, trace_level=TraceLevel.OFF
    )
    assert counts == {"update": 120, "propose": 120}


def test_views_with_false_claims_have_the_bits_of_python(
    generated_previews: Any,
    information_spread_scenario: Any,
    information_spread_config: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = copy.deepcopy(generated_previews()["scenario"])
    events = information_spread_scenario(duration=60, correction=True)
    document["duration_minutes"] = 60
    document["scheduled_events"] = events.model_dump(mode="json")["scheduled_events"]
    document["scheduled_events"][0]["payload"]["origin_agent_id"] = "A001"
    configs = document["model_configs"]
    configs["information_spread"] = configs["information_spread"] | information_spread_config()
    misinformed: list[int] = []
    python_rule = OfflineSimulationController._apply_due_proposals

    def counting(self: Any, view: WorldView, rng: Any) -> None:
        misinformed.append(sum(bool(a.misinformation_effect_sources) for a in view.agents.values()))
        python_rule(self, view, rng)

    monkeypatch.setattr(OfflineSimulationController, "_apply_due_proposals", counting)
    counts = _run_comparing(
        ScenarioSpec.model_validate(document), tmp_path, monkeypatch, trace_level=TraceLevel.OFF
    )
    assert counts == {"update": 60, "propose": 60}
    assert len(misinformed) == 60


def _world_with_views(scenario: Any) -> tuple[WorldState, OfflineSimulationController]:
    world = WorldState.from_scenario(scenario)
    world.model_state.setdefault("body_state_v1", create_body_state(world))
    return world, _controller(world)


def test_a_household_overrides_the_profile_group(generated_scenario: Any) -> None:
    world, controller = _world_with_views(generated_scenario)
    people = {agent_id: {"household_id": "H"} for agent_id in list(world.agents)[:2]}
    third = list(world.agents)[2]
    people[third] = {"household_id": None}
    world.model_state["population_presence_v1"] = {"people": people}
    made = _rules().view_after_movement(controller, world)
    python = WorldView.from_world(world, include_state_hash=False)
    _assert_same(made, python)
    assert made.agents[list(world.agents)[0]].group_id == "H"
    assert made.agents[third].group_id is None
    assert made.group_members["H"] == tuple(sorted(people))[:2]


def test_nothing_python_is_made_until_a_reader_asks(generated_scenario: Any) -> None:
    world, controller = _world_with_views(generated_scenario)
    made = _rules().view_after_movement(controller, world)
    assert type(made) is WorldView
    assert made.minute_index == world.minute_index
    assert not {"agents", "facilities", "zone_members", "group_members"} & made.__dict__.keys()
    assert made.state_hash is None
    agents = made.agents
    assert made.__dict__["agents"] is agents
    assert made.agents is agents
    with pytest.raises(AttributeError, match="no attribute 'nothing'"):
        made.nothing  # noqa: B018
    # What a Python rule does to a view works on one, and what it makes is a plain view.
    moved = replace(made, minute_index=7)
    assert moved.minute_index == 7 and moved.agents.keys() == agents.keys()
    assert "_rust_view" not in moved.__dict__


def test_a_tick_with_the_kernels_makes_no_python_view_in_the_update_phase(
    generated_scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prior view is Rust's, so Python didn't make it, and no rule has asked for its people."""
    world = WorldState.from_scenario(generated_scenario)
    controller = _controller(world)
    engine = TickEngine(world, phase_handler=controller)
    seen: list[set[str]] = []
    run_propose = OfflineSimulationController._run_propose_phase

    def before_propose(self: Any, world_: Any, minute: int, rng: Any) -> None:
        seen.append(set(self._view_before_proposals.__dict__))
        run_propose(self, world_, minute, rng)

    monkeypatch.setattr(OfflineSimulationController, "_run_propose_phase", before_propose)
    for _ in range(3):
        engine.step()
    assert len(seen) == 3
    # The first tick's social step is Python's: its namespace doesn't exist yet.
    for fields in seen[1:]:
        assert "_rust_view" in fields and "agents" not in fields


def test_a_snapshot_stays_as_it_was_read(generated_scenario: Any) -> None:
    world, controller = _world_with_views(generated_scenario)
    made = _rules().view_after_movement(controller, world)
    python = WorldView.from_world(world, include_state_hash=False)
    agent = next(iter(world.agents.values()))
    agent.stress = 0.5 if agent.stress != 0.5 else 0.25
    world.model_state["body_state_v1"]["agents"][agent.id]["uncertainty"] = 0.625
    _assert_same(made, python)


def test_the_proposal_view_reads_again_what_the_update_phase_writes(
    generated_scenario: Any,
) -> None:
    world, controller = _world_with_views(generated_scenario)
    controller._view_before_proposals = _rules().view_after_movement(controller, world)
    python_prior = WorldView.from_world(world, include_state_hash=False)
    agent = next(iter(world.agents.values()))
    agent.stress = 0.5 if agent.stress != 0.5 else 0.25
    agent.trust = 0.75 if agent.trust != 0.75 else 0.25
    agent.needs.hunger = 0.5 if agent.needs.hunger != 0.5 else 0.25
    world.model_state["body_state_v1"]["agents"][agent.id]["uncertainty"] = 0.625
    made = _rules().view_for_proposals(controller, world, world.minute_index)
    assert controller._view_before_proposals is None
    _assert_same(made, WorldView.after_state_update(python_prior, world))
    _assert_same(made, WorldView.from_world(world))


def test_a_python_prior_of_a_proposal_view_stays_with_python(generated_scenario: Any) -> None:
    world, controller = _world_with_views(generated_scenario)
    prior = WorldView.from_world(world, include_state_hash=False)
    controller._view_before_proposals = prior
    with pytest.raises(native.Unsupported, match="prior view"):
        _rules().view_for_proposals(controller, world, world.minute_index)
    assert controller._view_before_proposals is prior


def test_python_refreshes_a_rust_prior_into_a_plain_view(generated_scenario: Any) -> None:
    world, controller = _world_with_views(generated_scenario)
    prior = _rules().view_after_movement(controller, world)
    refreshed = WorldView.after_state_update(prior, world)
    assert "_rust_view" not in refreshed.__dict__
    _assert_same(refreshed, WorldView.from_world(world))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda world: world.model_state["body_state_v1"]["agents"]["A001"].__setitem__(
                "uncertainty", 0
            ),
            "not a float",
        ),
        (
            lambda world: world.model_state["body_state_v1"]["agents"].__setitem__("A001", 1),
            "plain dict",
        ),
        (lambda world: setattr(next(iter(world.agents.values())), "stress", 0), "tainted"),
        (
            lambda world: setattr(next(iter(world.agents.values())), "zone_id", "nowhere"),
            "unknown zone",
        ),
        (
            lambda world: world.model_state.__setitem__(
                "population_presence_v1", {"people": {"A001": {"household_id": 7}}}
            ),
            "not a str",
        ),
    ],
)
def test_what_a_typed_field_cannot_hold_is_left_to_python(
    generated_scenario: Any, change: Any, reason: str
) -> None:
    world, controller = _world_with_views(generated_scenario)
    change(world)
    with pytest.raises(native.Unsupported, match=reason):
        _rules().view_after_movement(controller, world)


def test_a_context_that_does_not_parse_raises_python_error(generated_scenario: Any) -> None:
    world, controller = _world_with_views(generated_scenario)
    world.model_configs["population_context"] = {"people": {"A001": {"pet_count": -1}}}
    with pytest.raises(native.Unsupported, match="Python rule"):
        _rules().view_after_movement(controller, world)
    with pytest.raises(ValueError, match="pet_count"):
        UpdatePhase._view_after_movement(controller, world)


def test_a_changed_context_is_seen_at_the_next_view(generated_scenario: Any) -> None:
    world, controller = _world_with_views(generated_scenario)
    assert _rules().view_after_movement(controller, world).agents["A001"].roles == ("alone",)
    record = world.model_configs["population_context"]["people"]["A001"]
    record["roles"] = ["parent"]
    second = _rules().view_after_movement(controller, world)
    assert second.agents["A001"].roles == ("parent",)
    record["roles"] = ["child"]
    third = _rules().view_after_movement(controller, world)
    assert third.agents["A001"].roles == ("child",)
    _assert_same(third, WorldView.from_world(world, include_state_hash=False))


def test_a_changed_zone_modifier_or_a_new_cache_is_seen_at_the_next_view(
    generated_scenario: Any,
) -> None:
    world, controller = _world_with_views(generated_scenario)
    zone = next(iter(world.agents.values())).zone_id
    _rules().view_after_movement(controller, world)
    world.model_state["zone_environment_v1"]["modifiers"]["m"] = {
        "zone_id": zone,
        "start_minute": 0,
        "op": "set_lighting",
        "parameters": {"level": 0.25},
        "start_event_id": "e",
        "source_id": "s",
        "assumption_ids": [],
    }
    made = _rules().view_after_movement(controller, world)
    _assert_same(made, WorldView.from_world(world, include_state_hash=False))
    agent = next(a for a in made.agents.values() if a.zone_id == zone)
    assert agent.lighting_level == 0.25 and agent.condition_modifier_event_ids == ("e",)
    # What the runner does when model_configs changed.
    world.model_configs["zone_environment"]["noise_rest_efficiency_loss"] = 0.25
    controller._zone_environments = {}
    made = _rules().view_after_movement(controller, world)
    _assert_same(made, WorldView.from_world(world, include_state_hash=False))


def test_stressors_outages_and_sanitation_are_read_as_python_reads_them(
    generated_scenario: Any,
) -> None:
    world, controller = _world_with_views(generated_scenario)
    facility = next(iter(world.facilities.values()))
    zones = sorted(world.zones)
    world.model_state["infrastructure_v1"] = {
        "environmental_stressors": {
            "b": {
                "active": True,
                "zone_id": zones[0],
                "stress_per_minute": 0.1,
                "start_event_id": "z",
                "source_id": "second",
            },
            "a": {
                "active": True,
                "zone_id": zones[0],
                "stress_per_minute": 0.2,
                "start_event_id": "y",
                "source_id": "first",
            },
            "c": {
                "active": False,
                "zone_id": zones[0],
                "stress_per_minute": 9.0,
                "start_event_id": "x",
                "source_id": "off",
            },
            "d": {
                "active": True,
                "zone_id": "no such zone",
                "stress_per_minute": 9.0,
                "start_event_id": "w",
                "source_id": "elsewhere",
            },
        },
        "outages": {
            "o": {"active": True, "affected_facility_ids": [facility.id], "start_event_id": "o2"},
            "p": {"active": False, "affected_facility_ids": [facility.id], "start_event_id": "o1"},
        },
    }
    facility.kind = "Water_Station"
    world.model_state["operational_events_v1"] = {
        "water_outages": {
            "by_zone": {
                "outage_event_id": "o3",
                "active": True,
                "target_kind": "zone",
                "target_id": facility.zone_id,
            },
            "by_id": {
                "outage_event_id": "o0",
                "active": True,
                "target_kind": "facility",
                "target_id": facility.id,
            },
            "other": {
                "outage_event_id": "o9",
                "active": True,
                "target_kind": "facility",
                "target_id": "another",
            },
        }
    }
    staff = world.model_state.setdefault("staff_operations_v1", {})
    staff.setdefault("zones", {}).setdefault(zones[0], {})["sanitation"] = 0.25
    made = _rules().view_after_movement(controller, world)
    _assert_same(made, WorldView.from_world(world, include_state_hash=False))
    assert made.facilities[facility.id].facility_event_context_ids == ("o0", "o2", "o3")
    in_first_zone = [a for a in made.agents.values() if a.zone_id == zones[0]]
    assert not in_first_zone or in_first_zone[0].environmental_assumption_source_id == "first"


def test_the_rust_view_hash_covers_each_form_of_the_document(generated_scenario: Any) -> None:
    for schema_version in (None, 6):
        for information in (False, True):
            for life_context in (False, True):
                world = WorldState.from_scenario(generated_scenario)
                controller = _controller(world)
                if schema_version is not None:
                    world.schema_version = schema_version
                    world.model_configs.setdefault("resource_service", {"a": 1.5})
                if not life_context:
                    world.model_state.pop("body_state_v1", None)
                    world.model_configs.pop("population_context", None)
                    world.model_state.pop("population_context_v1", None)
                if information and "information_spread_v1" not in world.model_state:
                    continue
                if not information:
                    world.model_state.pop("information_spread_v1", None)
                made = _rules().view_for_proposals(controller, world, world.minute_index)
                python = WorldView.from_world(world)
                assert made.state_hash == python.state_hash


@pytest.mark.python_rules
def test_the_mark_runs_the_python_steps(generated_scenario: Any) -> None:
    assert OfflineSimulationController._view_after_movement is UpdatePhase._view_after_movement
    assert OfflineSimulationController._view_for_proposals is ProposePhase._view_for_proposals
    assert world_view.WorldView.from_world
    world, controller = _world_with_views(generated_scenario)
    assert "_rust_view" not in controller._view_after_movement(world).__dict__


def test_without_the_mark_the_kernels_run() -> None:
    assert OfflineSimulationController._view_after_movement is not UpdatePhase._view_after_movement
